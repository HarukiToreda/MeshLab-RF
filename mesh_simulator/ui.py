from __future__ import annotations

import csv
import io
import math
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageTk

from .geography import (
    MapDataService,
    OBSTACLE_IMPORT_MAX_AREA_M2,
    OVERTURE_VIEWPORT_BUILDING_LIMIT,
    TILE_LAYERS,
    WEB_MERCATOR_WORLD_M,
    choose_tile_zoom,
    grayscale_map_tile,
    latlon_to_mercator,
    latlon_to_world,
    mercator_to_latlon,
    mercator_to_tile,
    obstacle_import_plan,
    tile_bounds_mercator,
    tile_size_m,
    world_viewport_to_mercator_bounds,
    world_to_latlon,
)
from .live_radio import LiveNode, LiveRadioClient, SerialPort, list_serial_ports
from .model import (
    CORE_PORTS,
    HARDWARE_POWER_PROFILE_KEYS,
    OBSTACLE_DEFAULTS,
    PRESETS,
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
    new_id,
    scenario_from_file,
    scenario_to_file,
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
HUD_LAYER_TAG = "hud-layer"
SELECTED_OBSTACLE_TAG = "selected-obstacle"


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
) -> dict[str, list[tuple[float, float, str]]]:
    """Sample a deterministic RF reception boundary around every transmitter."""
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
    environment = scenario.environment
    model = model or PropagationModel(scenario)
    mismatch_reasons = {
        "frequency mismatch",
        "bandwidth mismatch",
        "spreading factor mismatch",
        "coding rate mismatch",
        "offline",
    }

    contours: dict[str, list[tuple[float, float, str]]] = {}
    for source_id in transmitter_ids:
        source = nodes[source_id]
        compatible_receivers = [
            node for node in scenario.nodes
            if node.id != source.id and model.radios_compatible(source, node)[0]
        ]
        receiver_reference = compatible_receivers[len(compatible_receivers) // 2] if compatible_receivers else source
        probe = Node(
            id=f"coverage-probe-{source.id}",
            name="Coverage probe",
            x=source.x,
            y=source.y,
            elevation_m=source.elevation_m,
            antenna_height_m=receiver_reference.antenna_height_m,
            antenna_gain_dbi=receiver_reference.antenna_gain_dbi,
            cable_loss_db=receiver_reference.cable_loss_db,
            noise_figure_db=receiver_reference.noise_figure_db,
            channel=source.channel,
        )
        probe.radio = type(source.radio)(**vars(source.radio))
        maximum_range = model.unobstructed_range_m(source, probe) * 1.08
        points: list[tuple[float, float, str]] = []
        for index in range(angular_samples):
            angle = math.tau * index / angular_samples
            dx, dy = math.cos(angle), math.sin(angle)
            maximum = maximum_range
            probe.x = source.x + dx * maximum
            probe.y = source.y + dy * maximum
            ray_obstacles = model._candidate_obstacles(source, probe)

            def sample(distance: float):
                probe.x = source.x + dx * distance
                probe.y = source.y + dy * distance
                elevation = environment.terrain_elevation(probe.x, probe.y)
                probe.elevation_m = source.elevation_m if elevation is None else elevation
                return model.link(source, probe, obstacle_candidates=ray_obstacles)

            far_link = sample(maximum)
            if far_link.compatible and far_link.margin_db >= 0:
                reach = maximum
                boundary_kind = "threshold"
            else:
                low, high = 0.0, maximum
                failed_link = far_link
                for _iteration in range(14):
                    midpoint = (low + high) / 2.0
                    midpoint_link = sample(max(1.0, midpoint))
                    if midpoint_link.compatible and midpoint_link.margin_db >= 0:
                        low = midpoint
                    else:
                        high = midpoint
                        failed_link = midpoint_link
                reach = low
                boundary_kind = (
                    "blocked"
                    if not failed_link.compatible and failed_link.reason not in mismatch_reasons
                    else "threshold"
                )
            points.append((source.x + dx * reach, source.y + dy * reach, boundary_kind))
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
        self.animation_frame_count = 1
        self.sidebar_visible = False
        self.render_after: str | None = None
        self.simulation_thread: threading.Thread | None = None
        self.simulation_updates: queue.Queue[tuple[int, str, Any]] = queue.Queue()
        self.simulation_request_id = 0
        self.simulation_contours_complete = True
        self.results_populated = True
        self.probe_links = tk.BooleanVar(value=False)
        self.show_drops = tk.BooleanVar(value=True)
        self.map_visible = tk.BooleanVar(value=True)
        self.terrain_only_view = tk.BooleanVar(value=False)
        self.unit_system = tk.StringVar(value="Imperial")
        self.hop_line_vars = {hop: tk.BooleanVar(value=True) for hop in range(1, 8)}
        self.results_stale = False
        self.status_var = tk.StringVar(value="Ready")
        self.object_vars: dict[str, tk.Variable] = {}
        self.env_vars: dict[str, tk.Variable] = {}
        self.packet_vars: dict[str, tk.Variable] = {}
        self.map_service = MapDataService()
        self.map_tile_bytes: dict[tuple[str, int, int, int], bytes] = {}
        self.map_tile_images: dict[tuple[str, int, int, int, int], Image.Image] = {}
        self.map_tile_failures: set[tuple[str, int, int, int]] = set()
        self.obstacle_layer_image: ImageTk.PhotoImage | None = None
        self.node_label_layout: dict[str, tuple[float, float, float, float, float, float]] = {}
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
        self.terrain_request_id = 0

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
        style.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=0, padding=(9, 6))
        style.map("TButton", background=[("active", "#1d3553"), ("pressed", "#244465")])
        style.configure("Accent.TButton", background="#168cd1", foreground="white", padding=(12, 7))
        style.map("Accent.TButton", background=[("active", "#22a9ef"), ("pressed", "#117ab7")])
        style.configure("Danger.TButton", background="#4b2130", foreground="#ffb6c0")
        style.map("Danger.TButton", background=[("active", "#713044")])
        style.configure("Tool.TButton", background="#0d1b2e", padding=(9, 7))
        style.configure("ActiveTool.TButton", background="#164c70", foreground="#dff6ff", padding=(9, 7))
        style.configure("Tool.TMenubutton", background="#0d1b2e", foreground=TEXT, padding=(9, 7))
        style.configure("TEntry", fieldbackground=ENTRY, foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER, padding=5)
        style.configure("TCombobox", fieldbackground=ENTRY, background=ENTRY, foreground=TEXT, arrowcolor=TEXT, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", ENTRY)], selectbackground=[("readonly", ENTRY)])
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", background="#0c1726", foreground=MUTED, padding=(12, 7), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", TEXT)])
        style.configure(
            "Treeview",
            background="#0b1625",
            fieldbackground="#0b1625",
            foreground="#dbe8f7",
            rowheight=24,
            borderwidth=0,
        )
        style.configure("Treeview.Heading", background="#15253a", foreground="#a9c1dc", relief="flat", padding=4)
        style.map("Treeview", background=[("selected", "#174e72")], foreground=[("selected", "white")])
        style.configure("TPanedwindow", background=BORDER)
        style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)
        style.configure("Horizontal.TScrollbar", background=PANEL_2, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)

    def _create_hop_lines_menu(self, parent: tk.Misc) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
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
        menubar = tk.Menu(self.root, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73", activeforeground="white")
        file_menu = tk.Menu(menubar, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
        file_menu.add_command(label="New scenario", accelerator="Ctrl+N", command=self.new_scenario)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_scenario)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_scenario)
        file_menu.add_command(label="Save as…", accelerator="Ctrl+Shift+S", command=self.save_scenario_as)
        file_menu.add_command(label="Export results CSV…", command=self.export_results)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
        edit_menu.add_command(label="Add node", accelerator="N", command=lambda: self.set_tool("node"))
        edit_menu.add_command(label="Add random nodesâ€¦", command=self.add_random_nodes)
        edit_menu.add_separator()
        edit_menu.add_command(label="Duplicate selected", accelerator="Ctrl+D", command=self.duplicate_selected)
        edit_menu.add_command(label="Delete selected", accelerator="Del", command=self.delete_selected)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
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
        units_menu = tk.Menu(view_menu, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
        for units in ("Imperial", "Metric"):
            units_menu.add_radiobutton(
                label=units, value=units, variable=self.unit_system, command=self._units_changed
            )
        view_menu.add_cascade(label="Units", menu=units_menu)
        view_menu.add_checkbutton(label="Probe selected node links", variable=self.probe_links, command=self.render_canvas)
        view_menu.add_checkbutton(label="Show failed receptions", variable=self.show_drops, command=self.render_canvas)
        self.view_hop_menu = self._create_hop_lines_menu(view_menu)
        view_menu.add_cascade(label="Hop line visibility", menu=self.view_hop_menu)
        view_menu.add_command(label="Show / hide panels", accelerator="Tab", command=self.toggle_sidebar)
        view_menu.add_command(label="Clear packet traces", command=self.clear_results)
        menubar.add_cascade(label="View", menu=view_menu)

        sim_menu = tk.Menu(menubar, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
        sim_menu.add_command(label="Run packet", accelerator="Ctrl+Enter", command=self.run_simulation)
        sim_menu.add_command(label="Replay animation", command=self.replay_animation)
        sim_menu.add_command(label="Stop animation", command=self.stop_animation)
        menubar.add_cascade(label="Simulation", menu=sim_menu)

        help_menu = tk.Menu(menubar, tearoff=False, bg=PANEL_2, fg=TEXT, activebackground="#1d4f73")
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
        self.send_button = ttk.Button(bar, text="▶  Send packet", style="Accent.TButton", command=self.run_simulation)
        self.send_button.pack(side="right", padx=12, pady=6)
        self.clear_hops_button = ttk.Button(
            bar, text="Clear hops", style="Tool.TButton", command=self.clear_results, state="disabled"
        )
        self.clear_hops_button.pack(side="right", padx=2)
        ttk.Button(bar, text="☰  Panels", style="Tool.TButton", command=self.toggle_sidebar).pack(side="right", padx=2)
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
        ttk.Checkbutton(bar, text="Link probe", variable=self.probe_links, command=self.render_canvas).pack(side="right", padx=8)
        self.set_tool("select")

    def _build_layout(self) -> None:
        self.workspace = ttk.Frame(self.root, style="Root.TFrame")
        self.workspace.pack(fill="both", expand=True)
        self.canvas_panel = ttk.Frame(self.workspace)
        self.canvas_panel.pack(side="left", fill="both", expand=True)
        self.sidebar = ttk.Frame(self.workspace, width=390)
        self.sidebar.pack_propagate(False)
        self.sidebar_tabs = ttk.Notebook(self.sidebar)
        self.sidebar_tabs.pack(fill="both", expand=True)

        self.scene_panel = ttk.Frame(self.sidebar_tabs)
        self.object_scroll = ScrollFrame(self.sidebar_tabs)
        self.environment_scroll = ScrollFrame(self.sidebar_tabs)
        self.packet_scroll = ScrollFrame(self.sidebar_tabs)
        self.live_panel = ttk.Frame(self.sidebar_tabs)
        self.results_panel = ttk.Frame(self.sidebar_tabs)
        self.sidebar_tabs.add(self.scene_panel, text="Scene")
        self.sidebar_tabs.add(self.object_scroll, text="Properties")
        self.sidebar_tabs.add(self.environment_scroll, text="World")
        self.sidebar_tabs.add(self.packet_scroll, text="Packet")
        self.sidebar_tabs.add(self.live_panel, text="Live Radio")
        self.sidebar_tabs.add(self.results_panel, text="Results")
        self._build_scene_panel()
        self._build_live_panel()
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

    def toggle_sidebar(self) -> None:
        if self.sidebar_visible:
            self.sidebar.pack_forget()
            self.sidebar_visible = False
        else:
            self.sidebar.pack(side="right", fill="y")
            self.sidebar_visible = True
        self.root.after_idle(self.render_canvas)

    def show_sidebar_tab(self, name: str) -> None:
        if not self.sidebar_visible:
            self.sidebar.pack(side="right", fill="y")
            self.sidebar_visible = True
        tabs = {"Scene": 0, "Properties": 1, "World": 2, "Packet": 3, "Live Radio": 4, "Results": 5}
        self.sidebar_tabs.select(tabs[name])
        self.root.after_idle(self.render_canvas)

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
        self.live_port_picker.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.live_refresh_button = ttk.Button(connection, text="Refresh", command=self.refresh_live_ports)
        self.live_refresh_button.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self.live_connect_button = ttk.Button(
            connection, text="Connect", style="Accent.TButton", command=self.connect_live_radio
        )
        self.live_connect_button.grid(row=1, column=1, sticky="ew", padx=3)
        self.live_disconnect_button = ttk.Button(
            connection, text="Disconnect", command=self.disconnect_live_radio, state="disabled"
        )
        self.live_disconnect_button.grid(row=1, column=2, sticky="ew", padx=(3, 0))
        for column in range(3):
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

    def refresh_live_ports(self) -> None:
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
                "Connect the radio by USB, press Refresh, then choose its COM port.",
                parent=self.root,
            )
            return
        self.live_connection_ready = False
        self.live_connect_button.configure(state="disabled")
        self.live_refresh_button.configure(state="disabled")
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
        self.live_refresh_button.configure(state="normal")
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
        env.initial_view_width_m = max(6_000.0, span_x * 1.35)
        env.initial_view_height_m = max(4_200.0, span_y * 1.35)
        env.map_center_lat, env.map_center_lon = mercator_to_latlon(
            (min_x + max_x) / 2.0,
            (min_y + max_y) / 2.0,
        )
        env.map_configured = True
        env.map_layer = self.map_layer_var.get() if self.map_layer_var.get() in TILE_LAYERS else "Topographic"
        self._clear_terrain_grid()
        self.map_visible.set(True)
        self.map_tile_images.clear()
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
        entry = ttk.Entry(search, textvariable=self.map_search_var, width=34)
        entry.grid(row=0, column=0, columnspan=2, padx=(7, 3), pady=(7, 4), sticky="ew")
        entry.bind("<Return>", lambda _event: self.search_map())
        self.map_search_button = ttk.Button(search, text="Search map", command=self.search_map)
        self.map_search_button.grid(row=0, column=2, padx=(3, 7), pady=(7, 4))
        layer = ttk.Combobox(
            search,
            textvariable=self.map_layer_var,
            values=["Topographic", "Street"],
            state="readonly",
            width=14,
        )
        layer.grid(row=1, column=0, padx=(7, 3), pady=(3, 7), sticky="w")
        layer.bind("<<ComboboxSelected>>", self._map_layer_changed)
        self.osm_import_button = ttk.Button(search, text="Import obstacles", command=self.import_osm_obstacles)
        self.osm_import_button.grid(
            row=1,
            column=1,
            columnspan=2,
            padx=(3, 7),
            pady=(3, 7),
            sticky="ew",
        )
        self.map_canvas_toggle = tk.Checkbutton(
            search,
            text="Show map tiles",
            variable=self.map_visible,
            command=self._map_visibility_changed,
            bg="#081321",
            fg=TEXT,
            activebackground="#081321",
            activeforeground=TEXT,
            selectcolor="#081321",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
        )
        self.map_canvas_toggle.grid(row=2, column=0, columnspan=2, padx=7, pady=(0, 7), sticky="w")
        units = ttk.Combobox(
            search,
            textvariable=self.unit_system,
            values=["Imperial", "Metric"],
            state="readonly",
            width=9,
        )
        units.grid(row=2, column=2, padx=(3, 7), pady=(0, 7), sticky="e")
        units.bind("<<ComboboxSelected>>", self._units_changed)
        self.terrain_only_toggle = tk.Checkbutton(
            search,
            text="Terrain only · hide streets, highways, and labels",
            variable=self.terrain_only_view,
            command=self._terrain_only_changed,
            bg="#081321",
            fg=TEXT,
            activebackground="#081321",
            activeforeground=TEXT,
            selectcolor="#081321",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI Semibold", 9),
        )
        self.terrain_only_toggle.grid(row=3, column=0, columnspan=3, padx=7, pady=(0, 7), sticky="w")
        self.obstacle_progress_frame = tk.Frame(search, bg="#081321")
        self.obstacle_progress_frame.grid(row=4, column=0, columnspan=3, padx=7, pady=(0, 7), sticky="ew")
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
            length=320,
        )
        self.obstacle_progress_bar.grid(row=1, column=0, sticky="ew")
        self.obstacle_progress_frame.grid_remove()
        self.canvas.bind("<Configure>", lambda _e: self.schedule_render())
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
        if (self.scenario.nodes or self.scenario.obstacles) and not messagebox.askyesno(
            "Move geographic reference?",
            "Searching a new location reanchors the existing objects to that map. "
            "Their relative positions are preserved, but their real-world coordinates change.",
            parent=self.root,
        ):
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
            requested_width = abs(east_x - west_x) * 1.25
            requested_height = abs(north_y - south_y) * 1.25
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

    def _map_layer_changed(self, _event: tk.Event | None = None) -> None:
        layer = self.map_layer_var.get()
        if layer not in {"Topographic", "Street"}:
            return
        self.scenario.environment.map_layer = layer
        self.map_tile_images.clear()
        self.map_tile_failures.clear()
        self.mark_dirty()
        self.render_canvas()

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
        mercator_x = center_x + x
        mercator_y = center_y - y
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

    def import_osm_obstacles(self) -> None:
        env = self.scenario.environment
        if not env.map_configured:
            messagebox.showinfo("Search first", "Search for a real-world location before importing obstacles.", parent=self.root)
            return
        left, top = self.screen_to_world(0, 0)
        right, bottom = self.screen_to_world(self.canvas.winfo_width(), self.canvas.winfo_height())
        left, right = min(left, right), max(left, right)
        top, bottom = min(top, bottom), max(top, bottom)
        area_m2 = max(0.0, right - left) * max(0.0, bottom - top)
        if area_m2 <= 0:
            return
        if area_m2 > OBSTACLE_IMPORT_MAX_AREA_M2:
            messagebox.showinfo(
                "Zoom in before importing",
                (
                    f"The visible area is {self.format_area(area_m2)}. "
                    "Zoom in until the visible area is "
                    f"{self.format_area(OBSTACLE_IMPORT_MAX_AREA_M2)} or less, "
                    "then import obstacles again.\n\n"
                    "This limit applies only to obstacle imports; the map and "
                    "terrain can remain zoomed out farther."
                ),
                parent=self.root,
            )
            return
        visible_width_m = max(1.0, right - left)
        visible_height_m = max(1.0, bottom - top)
        import_columns, import_rows, building_limit, spatially_sampled = obstacle_import_plan(
            visible_width_m, visible_height_m
        )
        north, west = world_to_latlon(left, top, env.map_center_lat, env.map_center_lon)
        south, east = world_to_latlon(
            right, bottom, env.map_center_lat, env.map_center_lon
        )
        self.osm_import_button.configure(state="disabled")
        self._set_obstacle_progress("Preparing geographic cells…", 2.0)
        self.status_var.set(
            f"Importing the full visible {self.format_area(area_m2)} in "
            f"{import_columns}×{import_rows} geographic cells…"
        )

        def worker() -> None:
            warnings: list[str] = []
            def building_progress(completed: int, total: int, phase: str) -> None:
                coverage_percent = 100.0 * completed / max(1, total)
                percent = 5.0 + 78.0 * completed / max(1, total)
                self.geo_results.put(
                    (
                        "obstacle_progress",
                        {
                            "value": percent,
                            "text": f"{phase} · coverage {coverage_percent:.0f}%",
                        },
                    )
                )

            try:
                try:
                    buildings = self.map_service.fetch_overture_buildings_for_viewport(
                        south,
                        west,
                        north,
                        east,
                        limit=building_limit,
                        columns=import_columns,
                        rows=import_rows,
                        progress_callback=building_progress,
                    )
                    building_source = "Overture"
                    if spatially_sampled:
                        warnings.append(
                            "Very large view: building footprints were spatially sampled across the complete area"
                        )
                except Exception as overture_error:
                    self.geo_results.put(
                        (
                            "obstacle_progress",
                            {
                                "value": 0,
                                "text": "Overture unavailable; trying OpenStreetMap…",
                                "indeterminate": True,
                            },
                        )
                    )
                    buildings = self.map_service.fetch_osm_obstacles(south, west, north, east)
                    warnings.append(f"Overture unavailable: {overture_error}")
                    self.geo_results.put(
                        (
                            "obstacles",
                            {
                                "elements": buildings,
                                "building_source": "OSM fallback",
                                "warnings": warnings,
                                "building_limit": building_limit,
                            },
                        )
                    )
                    return
                try:
                    self.geo_results.put(
                        (
                            "obstacle_progress",
                            {
                                "value": 0,
                                "text": f"Loaded {len(buildings):,} buildings · fetching forests…",
                                "indeterminate": True,
                            },
                        )
                    )
                    forests = self.map_service.fetch_osm_forests(south, west, north, east)
                except Exception as forest_error:
                    forests = []
                    warnings.append(f"OSM forests unavailable: {forest_error}")
                self.geo_results.put(
                    (
                        "obstacles",
                        {
                            "elements": buildings + forests,
                            "building_source": building_source,
                            "warnings": warnings,
                            "building_limit": building_limit,
                        },
                    )
                )
            except Exception as error:
                self.geo_results.put(("error", ("Obstacle import", error)))

        threading.Thread(target=worker, name="ObstacleImport", daemon=True).start()

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
        existing = {obstacle.osm_id for obstacle in self.scenario.obstacles if obstacle.osm_id}
        added = 0
        added_buildings = 0
        added_forests = 0
        skipped = 0
        self._set_obstacle_progress(f"Adding {len(elements):,} obstacle shapes…", 88.0)
        for element_index, element in enumerate(elements, start=1):
            if element_index == 1 or element_index % 50 == 0 or element_index == len(elements):
                percent = 88.0 + 11.0 * element_index / max(1, len(elements))
                self._set_obstacle_progress(
                    f"Adding obstacle shapes · {element_index:,}/{len(elements):,}",
                    percent,
                )
                self.root.update_idletasks()
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
            x_values = [point[0] for point in points]
            y_values = [point[1] for point in points]
            center_x = sum(x_values) / len(x_values)
            center_y = sum(y_values) / len(y_values)
            base_elevation = env.terrain_elevation(center_x, center_y) or 0.0
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
            self.selected_id = None
            self.mark_dirty()
            self._mark_results_stale()
            self.refresh_all()
        suffix = f" · {skipped} skipped/capped" if skipped else ""
        warning_suffix = f" · {'; '.join(warnings)}" if warnings else ""
        self.status_var.set(
            f"Imported {added_buildings} {building_source} buildings and "
            f"{added_forests} OSM forests{suffix}{warning_suffix}"
        )
        self._hide_obstacle_progress()
        if added:
            imported = self.scenario.obstacles[-added:]
            if any(
                not self._terrain_covers(
                    (obstacle.normalized()[0] + obstacle.normalized()[2]) / 2.0,
                    (obstacle.normalized()[1] + obstacle.normalized()[3]) / 2.0,
                )
                for obstacle in imported
            ):
                self.load_topography()

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
        events_frame = ttk.Frame(notebook)
        nodes_frame = ttk.Frame(notebook)
        links_frame = ttk.Frame(notebook)
        notebook.add(events_frame, text="Event timeline")
        notebook.add(nodes_frame, text="Node delivery")
        notebook.add(links_frame, text="Link attempts")

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

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-n>", lambda _e: self.new_scenario())
        self.root.bind("<Control-o>", lambda _e: self.open_scenario())
        self.root.bind("<Control-s>", lambda _e: self.save_scenario())
        self.root.bind("<Control-Shift-S>", lambda _e: self.save_scenario_as())
        self.root.bind("<Control-d>", lambda _e: self.duplicate_selected())
        self.root.bind("<Delete>", lambda _e: self.delete_selected())
        self.root.bind("<Control-Return>", lambda _e: self.run_simulation())
        self.root.bind("<Key-f>", lambda _e: self.fit_view())
        self.root.bind("<Tab>", lambda _e: (self.toggle_sidebar(), "break")[1])
        self.root.bind("<Escape>", lambda _e: self.set_tool("select"))
        self.root.bind("<Key-n>", lambda _e: self.set_tool("node"))

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

    def _build_node_form(self, body: ttk.Frame, node: Node) -> None:
        self._form_header(body, node.name, f"Node !{node.node_num:08x} · {node.role}")
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
            "elevation_override": node.elevation_override,
            "antenna_height_m": self._display_length(node.antenna_height_m),
            "use_live_altitude": node.use_live_altitude,
            "power_profile": node.power_profile,
            "tx_power_dbm": node.tx_power_dbm,
            "antenna_gain_dbi": node.antenna_gain_dbi,
            "cable_loss_db": node.cable_loss_db,
            "noise_figure_db": node.noise_figure_db,
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
        env = self.scenario.environment
        if env.map_configured:
            latitude, longitude = world_to_latlon(
                node.x, node.y, env.map_center_lat, env.map_center_lon
            )
            ttk.Label(
                section,
                text=f"Latitude {latitude:.6f} · Longitude {longitude:.6f}",
                style="Muted.TLabel",
            ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(5, 2))
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
            ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(2, 4))
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
            ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(2, 4))

        section = self._section(body, "LoRa modem")
        preset_widget = self._field(section, 0, "Firmware preset", self.object_vars["preset"], list(PRESETS))
        preset_widget.bind("<<ComboboxSelected>>", self._preset_preview)
        self._field(section, 1, "Frequency (MHz)", self.object_vars["frequency_mhz"])
        self._field(section, 2, "Bandwidth (kHz)", self.object_vars["bandwidth_khz"])
        self._field(section, 3, "Spreading factor", self.object_vars["spreading_factor"])
        self._field(section, 4, "Coding rate 4/", self.object_vars["coding_rate"])

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
            bw, sf, cr = PRESETS[preset]
            self.object_vars["bandwidth_khz"].set(str(bw))
            self.object_vars["spreading_factor"].set(str(sf))
            self.object_vars["coding_rate"].set(str(cr))

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
        ttk.Button(actions, text="Apply changes", style="Accent.TButton", command=self.apply_object).pack(side="left")
        ttk.Button(actions, text="Duplicate", command=self.duplicate_selected).pack(side="left", padx=5)
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
        ttk.Button(body, text="▶  Run packet simulation", style="Accent.TButton", command=self.run_simulation).pack(
            fill="x", padx=12, pady=(16, 5)
        )
        ttk.Label(
            body,
            text=(
                "Broadcasts use managed flooding and never request an ACK. A direct message without a known route "
                "discovers one by flooding; request an ACK to learn it. Later DMs reuse the learned next-hop path."
            ),
            style="Muted.TLabel",
            wraplength=295,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 15))

    def _destination_preview(self, _event: tk.Event | None = None) -> None:
        if str(self.packet_vars["destination_name"].get()) != "BROADCAST":
            self.packet_vars["want_ack"].set(True)

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
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()
        if isinstance(obj, Node) and not self._terrain_covers(obj.x, obj.y):
            self.status_var.set("Node coordinates updated · refreshing terrain around current scene")
            self.load_topography()

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
        self.refresh_all()

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
                channel=str(self.packet_vars["channel"].get()),
            )
        except ValueError as error:
            messagebox.showerror("Invalid packet", str(error), parent=self.root)
            return None
        self.scenario.packet = packet
        return packet

    def run_simulation(self) -> None:
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
        self.send_button.configure(state="disabled", text="Finishing simulation…")
        self.clear_hops_button.configure(state="normal")
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
        else:
            self.result_status.configure(
                text=f"{len(self.last_result.reached)} of {len(self.scenario.nodes)} nodes heard the packet"
            )
            self.status_var.set("First-hop coverage ready · starting packet propagation")
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

    def clear_results(self) -> None:
        self.stop_animation()
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
            for variable in self.metric_vars.values():
                variable.set("—")
            self.result_status.configure(text="No packet sent")
        if hasattr(self, "send_button"):
            self.send_button.configure(state="normal", text="▶  Send packet")
        if hasattr(self, "clear_hops_button"):
            self.clear_hops_button.configure(state="disabled")
        self.status_var.set("Packet traces cleared · ready to send")
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
        for key, button in getattr(self, "tool_buttons", {}).items():
            button.configure(style="ActiveTool.TButton" if key == tool else "Tool.TButton")
        cursors = {"select": "arrow", "node": "crosshair"}
        if hasattr(self, "canvas"):
            self.canvas.configure(cursor=cursors.get(tool, "crosshair"))
        if tool == "select":
            self.status_var.set("Select and drag objects · right-drag pans · wheel zooms")
        elif tool == "node":
            self.status_var.set("Node tool stays active: click repeatedly to place nodes")
        elif tool == "Forest":
            self.status_var.set("Forest brush stays active: press and drag to paint forest")
        else:
            self.status_var.set(f"{tool} tool stays active: drag repeatedly to place obstructions")

    def world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        base = self._base_scale() * self.zoom
        return (x - self.view_x) * base, (y - self.view_y) * base

    def screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        base = self._base_scale() * self.zoom
        return x / max(1e-9, base) + self.view_x, y / max(1e-9, base) + self.view_y

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
        bounds = [obstacle.normalized() for obstacle in self.scenario.obstacles]
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

    def render_canvas(self) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete("all")
        env = self.scenario.environment
        c.configure(bg=env.background)
        visible_left, visible_top = self.screen_to_world(0, 0)
        visible_right, visible_bottom = self.screen_to_world(c.winfo_width(), c.winfo_height())
        visible_bounds = (
            min(visible_left, visible_right),
            min(visible_top, visible_bottom),
            max(visible_left, visible_right),
            max(visible_top, visible_bottom),
        )
        visible_obstacles = [
            obstacle
            for obstacle in self.scenario.obstacles
            if self._bounds_overlap(obstacle.normalized(), visible_bounds)
        ]
        self._draw_obstacle_layer(c, visible_obstacles)
        if self.probe_links.get():
            self._draw_probe_links(c)
        packet_start = len(c.find_all())
        self._draw_packet_links(c)
        self._draw_retained_coverage(c)
        self._tag_items_created_since(c, packet_start, PACKET_LAYER_TAG)
        node_start = len(c.find_all())
        self._prepare_node_label_layout()
        for node in self.scenario.nodes:
            self._draw_node(c, node)
        self._tag_items_created_since(c, node_start, NODE_LAYER_TAG)
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
        scale_start = len(c.find_all())
        self._draw_scale(c)
        self._tag_items_created_since(c, scale_start, HUD_LAYER_TAG)
        wave_start = len(c.find_all())
        self._draw_current_wave(c)
        self._tag_items_created_since(c, wave_start, CURRENT_WAVE_TAG)
        if self.map_visible.get():
            attribution_start = len(c.find_all())
            self._draw_map_attribution(c)
            self._tag_items_created_since(c, attribution_start, HUD_LAYER_TAG)

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
    def _tag_items_created_since(c: tk.Canvas, starting_count: int, tag: str) -> None:
        for item_id in c.find_all()[starting_count:]:
            c.addtag_withtag(tag, item_id)

    def _render_current_wave_frame(self) -> None:
        """Refresh only the moving ripple instead of rebuilding the geographic scene."""
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(CURRENT_WAVE_TAG)
        starting_count = len(c.find_all())
        self._draw_current_wave(c)
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

        wave_start = len(c.find_all())
        self._draw_current_wave(c)
        self._tag_items_created_since(c, wave_start, CURRENT_WAVE_TAG)

    def _compose_map_layer(self, c: tk.Canvas) -> Image.Image:
        canvas_width = max(1, c.winfo_width())
        canvas_height = max(1, c.winfo_height())
        composed = Image.new("RGB", (canvas_width, canvas_height), "#0a1524")
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
        pixel_size = max(32, round(tile_size_m(zoom) * scale))
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
                world_x = tile_mercator_left - center_x
                world_y = center_y - tile_mercator_top
                screen_x, screen_y = self.world_to_screen(world_x, world_y)
                try:
                    if pixel_size <= MAX_CACHED_TILE_PIXELS:
                        image_key = (*key, pixel_size)
                        tile_image = self.map_tile_images.get(image_key)
                        if tile_image is None:
                            tile_image = grayscale_map_tile(data, pixel_size).convert("RGB")
                            self.map_tile_images[image_key] = tile_image
                        composed.paste(tile_image, (round(screen_x), round(screen_y)))
                    else:
                        # Deep zoom can make a source tile tens of thousands
                        # of pixels wide. Enlarge only its visible portion.
                        image_key = (*key, 256)
                        tile_image = self.map_tile_images.get(image_key)
                        if tile_image is None:
                            tile_image = grayscale_map_tile(data, 256).convert("RGB")
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
        layer = self._compose_map_layer(c)
        drawing = ImageDraw.Draw(layer, "RGBA")
        scale = self._base_scale() * self.zoom
        view_x, view_y = self.view_x, self.view_y
        for obstacle in raster_obstacles:
            coordinates = [
                ((point[0] - view_x) * scale, (point[1] - view_y) * scale)
                for point in obstacle.points
            ]
            fill_rgb = ImageColor.getrgb(obstacle.color)
            outline_rgb = ImageColor.getrgb(self._lighten(obstacle.color))
            drawing.polygon(
                coordinates,
                fill=(*fill_rgb, 86 if obstacle.enabled else 42),
                outline=(*outline_rgb, 180 if obstacle.enabled else 100),
                width=1,
            )
        self.obstacle_layer_image = ImageTk.PhotoImage(layer)
        c.create_image(0, 0, image=self.obstacle_layer_image, anchor="nw")
        for obstacle in vector_obstacles:
            obstacle_start = len(c.find_all())
            self._draw_obstacle(c, obstacle)
            if obstacle.id == self.selected_id:
                self._tag_items_created_since(c, obstacle_start, SELECTED_OBSTACLE_TAG)

    def _render_selected_obstacle(self, obstacle: Obstacle) -> None:
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete(SELECTED_OBSTACLE_TAG)
        starting_count = len(c.find_all())
        self._draw_obstacle(c, obstacle)
        self._tag_items_created_since(c, starting_count, SELECTED_OBSTACLE_TAG)

    def _draw_obstacle(self, c: tk.Canvas, obstacle: Obstacle) -> None:
        selected = obstacle.id == self.selected_id
        outline = "#78ddff" if selected else self._lighten(obstacle.color)
        stipple = "gray50" if obstacle.enabled else "gray75"
        if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
            coordinates: list[float] = []
            for point_x, point_y in obstacle.points:
                screen_x, screen_y = self.world_to_screen(point_x, point_y)
                coordinates.extend((screen_x, screen_y))
            c.create_polygon(
                *coordinates,
                fill=obstacle.color,
                outline=outline,
                width=3 if selected else 1,
                stipple="gray75" if obstacle.enabled else "gray50",
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
        c.create_rectangle(sx1, sy1, sx2, sy2, fill=obstacle.color, outline=outline, width=3 if selected else 1, stipple=stipple)
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

    def _draw_node(self, c: tk.Canvas, node: Node) -> None:
        x, y = self.world_to_screen(node.x, node.y)
        color = ROLE_COLORS.get(node.role, ACCENT)
        if not node.online:
            color = "#526175"
        unreached = self.last_result is not None and node.id not in self.last_result.reached
        if unreached and node.online:
            color = "#77818d"
        selected = node.id == self.selected_id
        selected_path = self._selected_packet_path()
        path_focus = selected_path is not None
        on_selected_path = not path_focus or node.id in selected_path or selected
        if not on_selected_path:
            color = "#4b5664"
        reached = (
            self.last_result
            and node.id in self.last_result.reached
            and node.id in self.animation_revealed_nodes
        )
        show_delivery = reached and on_selected_path
        infrastructure = node.role in {"ROUTER", "ROUTER_LATE", "REPEATER", "CLIENT_BASE", "ROUTER_CLIENT"}
        marker_radius = 11 if infrastructure else 7
        if show_delivery:
            info = self.last_result.reached[node.id]
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
            hop = int(self.last_result.reached[node.id].get("hop", 0))
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

    def _draw_probe_links(self, c: tk.Canvas) -> None:
        selected = self.get_selected()
        if not isinstance(selected, Node):
            return
        model = PropagationModel(self.scenario)
        for target in self.scenario.nodes:
            if target.id == selected.id:
                continue
            link = model.link(selected, target)
            x1, y1 = self.world_to_screen(selected.x, selected.y)
            x2, y2 = self.world_to_screen(target.x, target.y)
            color = GREEN if link.margin_db >= 6 and link.compatible else AMBER if link.margin_db >= 0 and link.compatible else RED
            c.create_line(x1, y1, x2, y2, fill=color, width=1, dash=(4, 4))
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            c.create_text(
                mx,
                my,
                text=f"{link.margin_db:+.1f} dB · {link.rssi_dbm:.0f} RSSI",
                fill=color,
                font=("Consolas", 7),
            )

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
        if not self.path_focus_id or self.last_result is None:
            return None
        return packet_path_node_ids(self.last_result, self.path_focus_id)

    def _draw_retained_coverage(self, c: tk.Canvas) -> None:
        if not self.retained_coverage_transmitters:
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
        c.create_line(x, y, x + pixels, y, fill="#b9cbe0", width=2)
        c.create_line(x, y - 4, x, y + 4, fill="#b9cbe0", width=2)
        c.create_line(x + pixels, y - 4, x + pixels, y + 4, fill="#b9cbe0", width=2)
        c.create_text(
            x + pixels / 2, y - 9, text=self.format_distance(nice_meters), fill=MUTED, font=("Segoe UI", 8)
        )

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
        if self.tool == "Forest":
            x, y = self.drag_start_world
            self.temp_forest_points = [[x, y]]
            self.render_canvas()
            return
        if self.tool in OBSTACLE_DEFAULTS:
            x, y = self.drag_start_world
            self.temp_obstacle = (x, y, x, y)
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
            self.render_canvas()
            return
        if self.tool in OBSTACLE_DEFAULTS:
            x0, y0 = self.drag_start_world
            self.temp_obstacle = (x0, y0, wx, wy)
            self.render_canvas()
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
        self.drag_start_screen = None
        self.drag_start_world = None
        self.drag_object_origin = None
        self.drag_object_points = None

    def _pan_down(self, event: tk.Event) -> None:
        self.pan_start = (event.x, event.y)
        self.pan_last_screen = (event.x, event.y)
        self.pan_origin = (self.view_x, self.view_y)
        self.canvas.configure(cursor="fleur")

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

    def _canvas_wheel(self, event: tk.Event) -> None:
        before = self.screen_to_world(event.x, event.y)
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.zoom = clamp(self.zoom * factor, MIN_CANVAS_ZOOM, MAX_CANVAS_ZOOM)
        after = self.screen_to_world(event.x, event.y)
        self.view_x += before[0] - after[0]
        self.view_y += before[1] - after[1]
        self.render_canvas()

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
        for obstacle in reversed(self.scenario.obstacles):
            if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
                world_x, world_y = self.screen_to_world(sx, sy)
                polygon = [(point[0], point[1]) for point in obstacle.points]
                if PropagationModel._point_in_polygon(world_x, world_y, polygon):
                    return obstacle
                continue
            if obstacle.kind == "Forest" and obstacle.shape == "brush" and obstacle.points:
                world_x, world_y = self.screen_to_world(sx, sy)
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
                world_x, world_y = self.screen_to_world(sx, sy)
                x_min, y_min, x_max, y_max = obstacle.normalized()
                triangle = [((x_min + x_max) / 2, y_min), (x_max, y_max), (x_min, y_max)]
                if PropagationModel._point_in_polygon(world_x, world_y, triangle):
                    return obstacle
                continue
            x1, y1 = self.world_to_screen(obstacle.x1, obstacle.y1)
            x2, y2 = self.world_to_screen(obstacle.x2, obstacle.y2)
            if min(x1, x2) <= sx <= max(x1, x2) and min(y1, y2) <= sy <= max(y1, y2):
                return obstacle
        return None

    def add_node(self, x: float, y: float) -> None:
        number = max([node.node_num for node in self.scenario.nodes] + [0]) + 1
        node = Node(
            name=f"Node {len(self.scenario.nodes) + 1}",
            node_num=number,
            x=x,
            y=y,
        )
        self._set_auto_node_elevation(node)
        if self.scenario.nodes:
            template = self.scenario.nodes[0]
            node.radio = type(template.radio)(**vars(template.radio))
            node.channel = template.channel
        self.scenario.nodes.append(node)
        if not self.scenario.packet.source_id:
            self.scenario.packet.source_id = node.id
        self.selected_id = node.id
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()

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
        self.refresh_all()

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
        self.refresh_all()

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
        self.refresh_all()
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
        self.select(copy.id)
        self.mark_dirty()
        self._mark_results_stale()
        self.refresh_all()

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
        self.refresh_all()

    def get_selected(self) -> Node | Obstacle | None:
        for obj in [*self.scenario.nodes, *self.scenario.obstacles]:
            if obj.id == self.selected_id:
                return obj
        return None

    def select(self, item_id: str | None) -> None:
        selection_changed = item_id != self.selected_id
        self.selected_id = item_id
        selected_object = self.get_selected()
        self.path_focus_id = (
            selected_object.id if isinstance(selected_object, Node) and self.last_result is not None else None
        )
        if selection_changed:
            self._build_object_form()
        self.render_canvas()
        if isinstance(selected_object, Node) and self.last_result is not None:
            path = packet_path_node_ids(self.last_result, selected_object.id)
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
        self.scene_tree.delete(*self.scene_tree.get_children())
        nodes_root = self.scene_tree.insert("", "end", iid="_nodes", text=f"  Nodes  ({len(self.scenario.nodes)})", open=True)
        for node in self.scenario.nodes:
            marker = "●" if node.online else "○"
            self.scene_tree.insert(nodes_root, "end", iid=node.id, text=f"  {marker}  {node.name}  ·  {node.role}")
        obstacles_root = self.scene_tree.insert(
            "", "end", iid="_obstacles", text=f"  Obstructions  ({len(self.scenario.obstacles)})", open=True
        )
        for obstacle in self.scenario.obstacles:
            self.scene_tree.insert(obstacles_root, "end", iid=obstacle.id, text=f"  ▣  {obstacle.name}  ·  {obstacle.kind}")
        if self.selected_id and self.scene_tree.exists(self.selected_id):
            self.scene_tree.selection_set(self.selected_id)

    def _scene_tree_select(self, _event: tk.Event) -> None:
        selected = self.scene_tree.selection()
        if selected and not selected[0].startswith("_") and selected[0] != self.selected_id:
            self.select(selected[0])

    def refresh_all(self) -> None:
        if hasattr(self, "map_layer_var"):
            self.map_layer_var.set(self.scenario.environment.map_layer)
        self.refresh_scene_tree()
        self._build_object_form()
        self._build_environment_form()
        self._build_packet_form()
        self.render_canvas()
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
        self.scenario = Scenario(name="Untitled scenario")
        self.file_path = None
        self.selected_id = None
        self.dirty = False
        self.map_tile_images.clear()
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
            self.scenario = scenario_from_file(path)
        except (OSError, ValueError, TypeError) as error:
            messagebox.showerror("Could not open scenario", str(error), parent=self.root)
            return
        self.file_path = path
        self.selected_id = None
        self.dirty = False
        self.map_tile_images.clear()
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
        if not self._confirm_discard():
            return
        self.stop_animation()
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
            "DMs use directed hop lines; a failed stored path is removed and falls back to flooding.\n\n"
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


def run() -> None:
    root = tk.Tk()
    app = MeshSimulatorApp(root)
    root.after(100, app.fit_view)
    root.mainloop()
