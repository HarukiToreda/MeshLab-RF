from __future__ import annotations

import heapq
import json
import math
import random
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


PRESETS: dict[str, tuple[float, int, int]] = {
    "LONG_FAST": (250.0, 11, 5),
    "LONG_SLOW": (125.0, 12, 8),
    "MEDIUM_SLOW": (250.0, 10, 5),
    "MEDIUM_FAST": (250.0, 9, 5),
    "SHORT_SLOW": (250.0, 8, 5),
    "SHORT_FAST": (250.0, 7, 5),
    "LONG_MODERATE": (125.0, 11, 8),
    "SHORT_TURBO": (500.0, 7, 5),
    "LONG_TURBO": (500.0, 11, 8),
    "LITE_FAST": (125.0, 9, 5),
    "LITE_SLOW": (125.0, 10, 5),
    "NARROW_FAST": (62.5, 7, 6),
    "NARROW_SLOW": (62.5, 8, 6),
    "TINY_FAST": (15.6, 7, 5),
    "TINY_SLOW": (15.6, 8, 6),
    "MEDIUM_TURBO": (500.0, 9, 5),
    "CUSTOM": (250.0, 11, 5),
}

ROLES = [
    "CLIENT",
    "CLIENT_MUTE",
    "ROUTER",
    "ROUTER_CLIENT",
    "REPEATER",
    "TRACKER",
    "SENSOR",
    "TAK",
    "CLIENT_HIDDEN",
    "LOST_AND_FOUND",
    "TAK_TRACKER",
    "ROUTER_LATE",
    "CLIENT_BASE",
]

REBROADCAST_MODES = [
    "ALL",
    "ALL_SKIP_DECODING",
    "LOCAL_ONLY",
    "KNOWN_ONLY",
    "NONE",
    "CORE_PORTNUMS_ONLY",
]

CORE_PORTS = {"TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP", "ROUTING_APP"}

ROLE_COLORS = {
    "CLIENT": "#37b7ff",
    "CLIENT_MUTE": "#79869a",
    "ROUTER": "#ffb020",
    "ROUTER_CLIENT": "#4ade80",
    "REPEATER": "#f97316",
    "TRACKER": "#2dd4bf",
    "SENSOR": "#67e8f9",
    "TAK": "#c084fc",
    "CLIENT_HIDDEN": "#94a3b8",
    "LOST_AND_FOUND": "#fb7185",
    "TAK_TRACKER": "#a78bfa",
    "ROUTER_LATE": "#facc15",
    "CLIENT_BASE": "#4ade80",
}

OBSTACLE_DEFAULTS = {
    # color, penetration dB, height m, loss/100m dB, behavior, max distance beyond obstacle m
    "Building": ("#8b5e4a", 18.0, 12.0, 0.3, "LIMIT_AFTER", 482.803),
    "Wall": ("#ef4444", 25.0, 4.0, 0.0, "ATTENUATE", 0.0),
    "Forest": ("#166534", 2.0, 18.0, 10.0, "ATTENUATE", 0.0),
    "Mountain": ("#64748b", 35.0, 180.0, 0.08, "BLOCK", 0.0),
    "Water": ("#0369a1", 3.0, 1.0, 0.02, "ATTENUATE", 0.0),
    "Custom": ("#7c3aed", 10.0, 10.0, 0.1, "ATTENUATE", 0.0),
}

REQUIRED_SNR = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
DEFAULT_MAP_CENTER_LAT = 40.9045
DEFAULT_MAP_CENTER_LON = -74.2099


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class HardwarePowerProfile:
    key: str
    recommended_dbm: float
    maximum_dbm: float | None
    description: str
    aliases: tuple[str, ...] = ()


HARDWARE_POWER_PROFILES: tuple[HardwarePowerProfile, ...] = (
    HardwarePowerProfile(
        "Generic SX1262 / LR1110 (22 dBm)",
        22.0,
        22.0,
        "Most current sub-GHz Meshtastic radios · about 158 mW",
        ("SX1262", "SX1268", "LR1110", "LR1121", "RAK4631", "HELTEC_V3", "HELTEC T114"),
    ),
    HardwarePowerProfile(
        "Generic SX127x / RF95 (20 dBm)",
        20.0,
        20.0,
        "Legacy sub-GHz radios · 100 mW",
        ("SX1272", "SX1276", "SX1278", "RF95"),
    ),
    HardwarePowerProfile(
        "2.4 GHz SX128x / LR1120 (13 dBm)",
        13.0,
        13.0,
        "Meshtastic 2.4 GHz radios · about 20 mW",
        ("SX1280", "SX1281", "SX128X", "LR1120", "LORA_24", "2.4GHZ"),
    ),
    HardwarePowerProfile(
        "RAK3401 / RAK13302 1W (30 dBm)",
        30.0,
        30.0,
        "Amplifier-equipped RAK3401 and WisMesh Repeater Mini V2 HP · 1 W",
        ("RAK3401", "RAK13302", "RAK_WISMESH_REPEATER_MINI_HP", "REPEATER MINI V2 HP"),
    ),
    HardwarePowerProfile(
        "LILYGO T-Beam 1W (30 dBm)",
        30.0,
        32.0,
        "1 W nominal profile; radio and PA hardware can reach 32 dBm",
        ("TBEAM_1_WATT", "T-BEAM 1W", "TBEAM 1W", "LILYGO T-BEAM 1W"),
    ),
    HardwarePowerProfile(
        "Station G2 (30 dBm)",
        30.0,
        31.0,
        "Amplifier-equipped Station G2 · about 1 W at the normal limit",
        ("STATION_G2", "STATION G2"),
    ),
    HardwarePowerProfile(
        "Heltec PA models (29 dBm)",
        29.0,
        29.0,
        "Wireless Tracker V2, Mesh Node T096, and Heltec V4 PA variants · about 0.8 W",
        ("HELTEC_WIRELESS_TRACKER_V2", "HELTEC_MESH_NODE_T096", "HELTEC_V4_R8"),
    ),
    HardwarePowerProfile(
        "E22-900M30S PA (29 dBm)",
        29.0,
        29.0,
        "SX1262 with the firmware's measured E22-900M30S PA gain · about 0.8 W",
        ("E22-900M30S", "EBYTE_E22_900M30S"),
    ),
    HardwarePowerProfile(
        "Custom / measured output",
        22.0,
        None,
        "Enter the radio's measured total conducted output",
    ),
)
HARDWARE_POWER_PROFILE_KEYS = [profile.key for profile in HARDWARE_POWER_PROFILES]
DEFAULT_HARDWARE_POWER_PROFILE = HARDWARE_POWER_PROFILES[0].key


def _normalized_hardware_name(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def hardware_power_profile(value: str) -> HardwarePowerProfile:
    """Return the closest conducted-output profile for a device, enum name, or radio family."""
    normalized = _normalized_hardware_name(value)
    for profile in HARDWARE_POWER_PROFILES:
        if normalized == _normalized_hardware_name(profile.key):
            return profile
        if any(_normalized_hardware_name(alias) in normalized for alias in profile.aliases):
            return profile
    return HARDWARE_POWER_PROFILES[0]


def dbm_to_watts(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0)


@dataclass
class RadioConfig:
    preset: str = "LONG_FAST"
    frequency_mhz: float = 906.875
    bandwidth_khz: float = 250.0
    spreading_factor: int = 11
    coding_rate: int = 5

    def apply_preset(self, preset: str) -> None:
        self.preset = preset
        if preset in PRESETS and preset != "CUSTOM":
            self.bandwidth_khz, self.spreading_factor, self.coding_rate = PRESETS[preset]


@dataclass
class Node:
    id: str = field(default_factory=lambda: new_id("node"))
    name: str = "Node"
    node_num: int = 1
    x: float = 1000.0
    y: float = 1000.0
    elevation_m: float = 0.0
    elevation_override: bool = False
    antenna_height_m: float = 2.0
    role: str = "CLIENT"
    rebroadcast_mode: str = "ALL"
    radio: RadioConfig = field(default_factory=RadioConfig)
    tx_power_dbm: float = 22.0
    antenna_gain_dbi: float = 2.15
    cable_loss_db: float = 0.5
    noise_figure_db: float = 6.0
    channel: str = "LongFast"
    online: bool = True
    favorite: bool = False
    notes: str = ""
    live_port: str = ""
    hardware_model: str = ""
    power_profile: str = DEFAULT_HARDWARE_POWER_PROFILE
    last_heard: int | None = None
    live_snr_db: float | None = None
    hops_away: int | None = None
    position_precision_bits: int | None = None
    use_live_altitude: bool = True
    reported_altitude_m: float | None = None
    reported_altitude_hae_m: float | None = None
    reported_altitude_source: str = ""
    reported_altitude_accuracy_m: float | None = None
    reported_altitude_usable: bool = True
    reported_altitude_status: str = ""

    @property
    def uses_reported_altitude(self) -> bool:
        return (
            self.use_live_altitude
            and self.reported_altitude_usable
            and self.reported_altitude_m is not None
            and math.isfinite(self.reported_altitude_m)
        )

    @property
    def antenna_z(self) -> float:
        """Absolute RF antenna elevation above mean sea level, in metres."""
        if self.uses_reported_altitude:
            return self.reported_altitude_m
        return self.elevation_m + self.antenna_height_m

    @property
    def effective_agl_m(self) -> float:
        """RF antenna height above the terrain elevation stored for this node."""
        return self.antenna_z - self.elevation_m


@dataclass
class Obstacle:
    id: str = field(default_factory=lambda: new_id("obs"))
    name: str = "Building"
    kind: str = "Building"
    x1: float = 1500.0
    y1: float = 1200.0
    x2: float = 2200.0
    y2: float = 1800.0
    height_m: float = 18.0
    base_elevation_m: float = 0.0
    attenuation_db: float = 12.0
    loss_per_100m_db: float = 0.3
    behavior: str = "LIMIT_AFTER"
    max_range_beyond_m: float = 482.803
    shape: str = "rectangle"
    points: list[list[float]] = field(default_factory=list)
    brush_radius_m: float = 150.0
    osm_id: str = ""
    enabled: bool = True
    color: str = "#8b5e4a"

    def normalized(self) -> tuple[float, float, float, float]:
        if self.points:
            xs = [point[0] for point in self.points]
            ys = [point[1] for point in self.points]
            radius = self.brush_radius_m if self.shape == "brush" else 0.0
            return min(xs) - radius, min(ys) - radius, max(xs) + radius, max(ys) + radius
        return min(self.x1, self.x2), min(self.y1, self.y2), max(self.x1, self.x2), max(self.y1, self.y2)


@dataclass
class Environment:
    initial_view_width_m: float = 10000.0
    initial_view_height_m: float = 7000.0
    coordinate_space: str = "CENTERED_MERCATOR"
    path_loss_exponent: float = 2.45
    shadowing_sigma_db: float = 2.0
    weather_loss_db: float = 0.0
    capture_threshold_db: float = 6.0
    stochastic: bool = True
    seed: int = 42
    grid_m: float = 500.0
    background: str = "#0b1220"
    map_configured: bool = True
    map_center_lat: float = DEFAULT_MAP_CENTER_LAT
    map_center_lon: float = DEFAULT_MAP_CENTER_LON
    map_layer: str = "Topographic"
    terrain_enabled: bool = True
    terrain_columns: int = 0
    terrain_rows: int = 0
    terrain_values: list[float] = field(default_factory=list)
    terrain_source: str = ""
    terrain_left_m: float = 0.0
    terrain_top_m: float = 0.0
    terrain_width_m: float = 0.0
    terrain_height_m: float = 0.0

    def terrain_bounds(self) -> tuple[float, float, float, float]:
        """Return cached terrain coverage, which is independent of workspace size."""
        if self.terrain_width_m > 0.0 and self.terrain_height_m > 0.0:
            return (
                self.terrain_left_m,
                self.terrain_top_m,
                self.terrain_left_m + self.terrain_width_m,
                self.terrain_top_m + self.terrain_height_m,
            )
        # Compatibility for Environment objects created directly by older code.
        return 0.0, 0.0, self.initial_view_width_m, self.initial_view_height_m

    def ground_elevation(self, x: float, y: float) -> float | None:
        """Interpolate loaded ground height independently of the RF terrain toggle."""
        left, top, right, bottom = self.terrain_bounds()
        if (
            self.terrain_columns < 2
            or self.terrain_rows < 2
            or len(self.terrain_values) != self.terrain_columns * self.terrain_rows
            or not (left <= x <= right and top <= y <= bottom)
        ):
            return None
        grid_x = (x - left) / max(1.0, right - left) * (self.terrain_columns - 1)
        grid_y = (y - top) / max(1.0, bottom - top) * (self.terrain_rows - 1)
        x0, y0 = math.floor(grid_x), math.floor(grid_y)
        x1, y1 = min(self.terrain_columns - 1, x0 + 1), min(self.terrain_rows - 1, y0 + 1)
        fx, fy = grid_x - x0, grid_y - y0
        def value(column: int, row: int) -> float:
            return self.terrain_values[row * self.terrain_columns + column]
        top = value(x0, y0) * (1.0 - fx) + value(x1, y0) * fx
        bottom = value(x0, y1) * (1.0 - fx) + value(x1, y1) * fx
        return top * (1.0 - fy) + bottom * fy

    def terrain_elevation(self, x: float, y: float) -> float | None:
        if not self.terrain_enabled:
            return None
        return self.ground_elevation(x, y)


@dataclass
class PacketConfig:
    source_id: str = ""
    destination_id: str = "BROADCAST"
    payload: str = "Hello mesh"
    payload_bytes: int = 32
    hop_limit: int = 3
    port: str = "TEXT_MESSAGE_APP"
    want_ack: bool = False
    want_response: bool = False
    channel: str = "LongFast"


@dataclass
class LiveMeshConfig:
    duration_minutes: int = 360
    traffic_profile: str = "FIRMWARE_LIKE"
    hop_limit: int = 3
    playback_seconds: int = 30
    nodeinfo_interval_minutes: float = 180.0
    telemetry_interval_minutes: float = 60.0
    router_telemetry_interval_minutes: float = 720.0
    sensor_interval_minutes: float = 60.0
    message_interval_minutes: float = 120.0


@dataclass
class LinkResult:
    source_id: str
    target_id: str
    distance_m: float
    rssi_dbm: float
    snr_db: float
    required_snr_db: float
    margin_db: float
    probability: float
    obstacle_loss_db: float
    path_loss_db: float
    compatible: bool
    reason: str
    obstacles: list[str] = field(default_factory=list)


@dataclass
class BeaconRay:
    """One radial sample of a beacon's signal, from the beacon outwards."""

    angle: float
    reach_m: float
    clear_reach_m: float
    kind: str  # "clear" | "weakened" | "blocked"
    obstacle_ids: list[str] = field(default_factory=list)


@dataclass
class BeaconProfile:
    """A full 360° snapshot of where a beacon's signal reaches, fades, or is blocked."""

    source_id: str
    x: float
    y: float
    rays: list[BeaconRay]
    blocking_obstacle_ids: list[str] = field(default_factory=list)
    weakening_obstacle_ids: list[str] = field(default_factory=list)
    max_reach_m: float = 0.0


@dataclass
class SimEvent:
    time_ms: float
    kind: str
    node_id: str
    peer_id: str = ""
    hop: int = 0
    rssi_dbm: float | None = None
    snr_db: float | None = None
    margin_db: float | None = None
    detail: str = ""
    decoded: bool = True
    airtime_ms: float = 0.0


@dataclass
class SimulationResult:
    events: list[SimEvent] = field(default_factory=list)
    links: list[LinkResult] = field(default_factory=list)
    reached: dict[str, dict[str, Any]] = field(default_factory=dict)
    transmissions: int = 0
    receptions: int = 0
    decoded: int = 0
    collisions: int = 0
    dropped: int = 0
    max_distance_m: float = 0.0
    total_airtime_ms: float = 0.0
    duration_ms: float = 0.0
    routing_mode: str = "BROADCAST_FLOOD"
    route_key: str = ""
    learned_route: list[str] = field(default_factory=list)
    invalidated_route_key: str = ""
    acknowledged: bool = False


@dataclass
class Scenario:
    name: str = "Untitled scenario"
    environment: Environment = field(default_factory=Environment)
    nodes: list[Node] = field(default_factory=list)
    obstacles: list[Obstacle] = field(default_factory=list)
    packet: PacketConfig = field(default_factory=PacketConfig)
    live_mesh: LiveMeshConfig = field(default_factory=LiveMeshConfig)
    learned_routes: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        environment_data = dict(data.get("environment", {}))
        legacy_coordinates = environment_data.get("coordinate_space") != "CENTERED_MERCATOR"
        legacy_width = float(environment_data.pop("width_m", 10_000.0))
        legacy_height = float(environment_data.pop("height_m", 7_000.0))
        environment_data.setdefault("initial_view_width_m", legacy_width)
        environment_data.setdefault("initial_view_height_m", legacy_height)
        known_environment_fields = Environment.__dataclass_fields__
        environment_data = {
            key: value for key, value in environment_data.items() if key in known_environment_fields
        }
        env = Environment(**environment_data)
        if not env.map_configured:
            env.map_configured = True
            if env.map_center_lat == 0.0 and env.map_center_lon == 0.0:
                env.map_center_lat = DEFAULT_MAP_CENTER_LAT
                env.map_center_lon = DEFAULT_MAP_CENTER_LON
        nodes = []
        for raw in data.get("nodes", []):
            raw = dict(raw)
            raw["radio"] = RadioConfig(**raw.get("radio", {}))
            nodes.append(Node(**raw))
        obstacles = []
        for obstacle_data in data.get("obstacles", []):
            raw = dict(obstacle_data)
            defaults = OBSTACLE_DEFAULTS.get(raw.get("kind", "Custom"), OBSTACLE_DEFAULTS["Custom"])
            raw.setdefault("behavior", defaults[4])
            raw.setdefault("max_range_beyond_m", defaults[5])
            raw.setdefault("shape", "rectangle")
            raw.setdefault("points", [])
            raw.setdefault("brush_radius_m", 150.0)
            obstacles.append(Obstacle(**raw))
        if legacy_coordinates:
            shift_x = legacy_width / 2.0
            shift_y = legacy_height / 2.0
            for node in nodes:
                node.x -= shift_x
                node.y -= shift_y
            for obstacle in obstacles:
                obstacle.x1 -= shift_x
                obstacle.x2 -= shift_x
                obstacle.y1 -= shift_y
                obstacle.y2 -= shift_y
                obstacle.points = [
                    [point[0] - shift_x, point[1] - shift_y]
                    for point in obstacle.points
                ]
            if env.terrain_values:
                env.terrain_left_m = -shift_x
                env.terrain_top_m = -shift_y
                env.terrain_width_m = legacy_width
                env.terrain_height_m = legacy_height
            env.coordinate_space = "CENTERED_MERCATOR"
        packet = PacketConfig(**data.get("packet", {}))
        live_mesh = LiveMeshConfig(**data.get("live_mesh", {}))
        learned_routes = {
            str(key): [str(node_id) for node_id in route]
            for key, route in data.get("learned_routes", {}).items()
            if isinstance(route, list)
        }
        return cls(
            name=data.get("name", "Untitled scenario"),
            environment=env,
            nodes=nodes,
            obstacles=obstacles,
            packet=packet,
            live_mesh=live_mesh,
            learned_routes=learned_routes,
        )

    @classmethod
    def from_json(cls, text: str) -> "Scenario":
        return cls.from_dict(json.loads(text))


def create_demo_scenario() -> Scenario:
    s = Scenario(name="Mountain relay demo")
    s.nodes = [
        Node(name="Trailhead", node_num=0xA1, x=700, y=3500, antenna_height_m=1.8, role="CLIENT"),
        Node(name="Ridge Router", node_num=0xB2, x=4250, y=1700, elevation_m=210, antenna_height_m=8, role="ROUTER",
             tx_power_dbm=27, antenna_gain_dbi=5.8),
        Node(name="Valley Team", node_num=0xC3, x=7300, y=3900, antenna_height_m=2, role="CLIENT"),
        Node(name="Weather Sensor", node_num=0xD4, x=5600, y=5600, antenna_height_m=3, role="SENSOR",
             rebroadcast_mode="NONE"),
        Node(name="Fallback Router", node_num=0xE5, x=2700, y=5450, elevation_m=80, antenna_height_m=5,
             role="ROUTER_LATE", tx_power_dbm=24, antenna_gain_dbi=3.0),
    ]
    s.obstacles = [
        Obstacle(name="Granite ridge", kind="Mountain", x1=2800, y1=2600, x2=5900, y2=3550, height_m=260,
                 attenuation_db=42, loss_per_100m_db=0.08, behavior="BLOCK", max_range_beyond_m=0,
                 color="#475569"),
        Obstacle(name="Pine forest", kind="Forest", x1=900, y1=4300, x2=3900, y2=6100, height_m=24,
                 attenuation_db=2, loss_per_100m_db=1.8, behavior="ATTENUATE", max_range_beyond_m=0,
                 color="#14532d"),
        Obstacle(name="Operations building", kind="Building", x1=6400, y1=2700, x2=7100, y2=3350, height_m=15,
                 attenuation_db=16, loss_per_100m_db=0.3, color="#7c4a3a"),
    ]
    s.packet = PacketConfig(source_id=s.nodes[0].id, destination_id="BROADCAST", payload="Check-in", payload_bytes=32,
                            hop_limit=3, channel="LongFast")
    return s


class PropagationModel:
    SPEED_OF_LIGHT = 299_792_458.0

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        bounds = [obstacle.normalized() for obstacle in scenario.obstacles]
        xs = [node.x for node in scenario.nodes]
        ys = [node.y for node in scenario.nodes]
        for left, top, right, bottom in bounds:
            xs.extend((left, right))
            ys.extend((top, bottom))
        span = max(
            (max(xs) - min(xs)) if xs else 0.0,
            (max(ys) - min(ys)) if ys else 0.0,
            6_400.0,
        )
        self._obstacle_cell_m = max(
            100.0,
            min(5000.0, span / 64.0),
        )
        self._obstacle_cells: dict[tuple[int, int], list[int]] = {}
        self._global_obstacle_indices: list[int] = []
        self._obstacle_bounds: dict[int, tuple[float, float, float, float]] = {}
        self._obstacle_polygons: dict[int, list[tuple[float, float]]] = {}
        for index, obstacle in enumerate(scenario.obstacles):
            x1, y1, x2, y2 = obstacle.normalized()
            obstacle_key = id(obstacle)
            self._obstacle_bounds[obstacle_key] = (x1, y1, x2, y2)
            if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
                self._obstacle_polygons[obstacle_key] = [
                    (point[0], point[1]) for point in obstacle.points
                ]
            cell_x1, cell_y1 = math.floor(x1 / self._obstacle_cell_m), math.floor(y1 / self._obstacle_cell_m)
            cell_x2, cell_y2 = math.floor(x2 / self._obstacle_cell_m), math.floor(y2 / self._obstacle_cell_m)
            cell_count = (cell_x2 - cell_x1 + 1) * (cell_y2 - cell_y1 + 1)
            if cell_count > 4096:
                self._global_obstacle_indices.append(index)
                continue
            for cell_x in range(cell_x1, cell_x2 + 1):
                for cell_y in range(cell_y1, cell_y2 + 1):
                    self._obstacle_cells.setdefault((cell_x, cell_y), []).append(index)

    def _candidate_obstacles(self, source: Node, target: Node) -> list[Obstacle]:
        """Return spatial-index candidates along the segment, preserving scenario order."""
        if not self.scenario.obstacles:
            return []
        if len(self.scenario.obstacles) <= 128:
            return self.scenario.obstacles
        dx, dy = target.x - source.x, target.y - source.y
        steps = max(1, math.ceil(max(abs(dx), abs(dy)) / self._obstacle_cell_m))
        indices = set(self._global_obstacle_indices)
        for step in range(steps + 1):
            progress = step / steps
            cell_x = math.floor((source.x + dx * progress) / self._obstacle_cell_m)
            cell_y = math.floor((source.y + dy * progress) / self._obstacle_cell_m)
            for nearby_x in range(cell_x - 1, cell_x + 2):
                for nearby_y in range(cell_y - 1, cell_y + 2):
                    indices.update(self._obstacle_cells.get((nearby_x, nearby_y), ()))
        return [self.scenario.obstacles[index] for index in sorted(indices)]

    @staticmethod
    def noise_floor(node: Node) -> float:
        return -174.0 + 10.0 * math.log10(max(1.0, node.radio.bandwidth_khz * 1000.0)) + node.noise_figure_db

    @staticmethod
    def sensitivity(node: Node) -> float:
        return PropagationModel.noise_floor(node) + REQUIRED_SNR.get(node.radio.spreading_factor, -7.5)

    def unobstructed_range_m(self, source: Node, target: Node) -> float:
        """Return the link-budget range at which an unobstructed receiver reaches zero margin."""
        frequency_hz = max(1.0, source.radio.frequency_mhz * 1_000_000.0)
        fspl_1m = 20.0 * math.log10(4.0 * math.pi * frequency_hz / self.SPEED_OF_LIGHT)
        received_budget = (
            source.tx_power_dbm
            + source.antenna_gain_dbi
            + target.antenna_gain_dbi
            - source.cable_loss_db
            - target.cable_loss_db
            - self.scenario.environment.weather_loss_db
        )
        allowed_path_loss = received_budget - self.sensitivity(target)
        exponent = max(0.1, self.scenario.environment.path_loss_exponent)
        distance = 10.0 ** ((allowed_path_loss - fspl_1m) / (10.0 * exponent))
        return max(1.0, min(2_000_000.0, distance))

    @staticmethod
    def airtime_ms(node: Node, payload_bytes: int) -> float:
        sf = max(5, min(12, int(node.radio.spreading_factor)))
        bw_hz = max(7800.0, node.radio.bandwidth_khz * 1000.0)
        cr_value = max(5, min(8, int(node.radio.coding_rate))) - 4
        total_bytes = max(1, payload_bytes + 16)
        symbol_time = (2**sf) / bw_hz
        low_data_rate = 1 if symbol_time >= 0.016 else 0
        numerator = 8 * total_bytes - 4 * sf + 28 + 16
        denominator = 4 * (sf - 2 * low_data_rate)
        payload_symbols = 8 + max(math.ceil(numerator / denominator) * (cr_value + 4), 0)
        preamble_symbols = 16 + 4.25
        return (preamble_symbols + payload_symbols) * symbol_time * 1000.0

    @staticmethod
    def _segment_rect_intersection(
        ax: float, ay: float, bx: float, by: float, rect: tuple[float, float, float, float]
    ) -> tuple[float, float | None, float | None]:
        x_min, y_min, x_max, y_max = rect
        # Fast reject: if the segment's bounding box sits wholly to one side of the
        # rectangle it cannot cross it.  This skips the full clip for the many
        # candidate obstacles that a long ray passes nowhere near.
        if (
            (ax < x_min and bx < x_min)
            or (ax > x_max and bx > x_max)
            or (ay < y_min and by < y_min)
            or (ay > y_max and by > y_max)
        ):
            return 0.0, None, None
        dx, dy = bx - ax, by - ay
        p = (-dx, dx, -dy, dy)
        q = (ax - x_min, x_max - ax, ay - y_min, y_max - ay)
        t0, t1 = 0.0, 1.0
        for pi, qi in zip(p, q):
            if abs(pi) < 1e-12:
                if qi < 0:
                    return 0.0, None, None
                continue
            r = qi / pi
            if pi < 0:
                if r > t1:
                    return 0.0, None, None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return 0.0, None, None
                t1 = min(t1, r)
        if t1 < t0:
            return 0.0, None, None
        planar = math.hypot(dx, dy)
        return planar * max(0.0, t1 - t0), (t0 + t1) / 2.0, t1

    @staticmethod
    def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
        inside = False
        j = len(polygon) - 1
        for i, (xi, yi) in enumerate(polygon):
            xj, yj = polygon[j]
            if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _line_edge_t(
        ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
    ) -> float | None:
        rx, ry = bx - ax, by - ay
        sx, sy = dx - cx, dy - cy
        denominator = rx * sy - ry * sx
        if abs(denominator) < 1e-12:
            return None
        qx, qy = cx - ax, cy - ay
        t = (qx * sy - qy * sx) / denominator
        u = (qx * ry - qy * rx) / denominator
        return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None

    @classmethod
    def _segment_polygon_intersection(
        cls, ax: float, ay: float, bx: float, by: float, polygon: list[tuple[float, float]]
    ) -> tuple[float, float | None, float | None]:
        parameters: list[float] = []
        if cls._point_in_polygon(ax, ay, polygon):
            parameters.append(0.0)
        if cls._point_in_polygon(bx, by, polygon):
            parameters.append(1.0)
        for index, (cx, cy) in enumerate(polygon):
            dx, dy = polygon[(index + 1) % len(polygon)]
            t = cls._line_edge_t(ax, ay, bx, by, cx, cy, dx, dy)
            if t is not None:
                parameters.append(t)
        if not parameters:
            return 0.0, None, None
        t0, t1 = min(parameters), max(parameters)
        planar = math.hypot(bx - ax, by - ay)
        return planar * max(0.0, t1 - t0), (t0 + t1) / 2.0, t1

    @staticmethod
    def _point_segment_distance_sq(
        px: float, py: float, ax: float, ay: float, bx: float, by: float
    ) -> float:
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return (px - ax) ** 2 + (py - ay) ** 2
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        qx, qy = ax + t * dx, ay + t * dy
        return (px - qx) ** 2 + (py - qy) ** 2

    @classmethod
    def _segment_brush_intersection(
        cls, ax: float, ay: float, bx: float, by: float, points: list[list[float]], radius: float
    ) -> tuple[float, float | None, float | None]:
        if not points:
            return 0.0, None, None
        planar = math.hypot(bx - ax, by - ay)
        samples = max(12, min(800, math.ceil(planar / max(10.0, radius * 0.35))))
        radius_sq = radius * radius
        inside_parameters: list[float] = []
        stroke_segments = list(zip(points, points[1:])) or [(points[0], points[0])]
        for index in range(samples + 1):
            t = index / samples
            px, py = ax + (bx - ax) * t, ay + (by - ay) * t
            if any(
                cls._point_segment_distance_sq(px, py, first[0], first[1], second[0], second[1]) <= radius_sq
                for first, second in stroke_segments
            ):
                inside_parameters.append(t)
        if not inside_parameters:
            return 0.0, None, None
        t0, t1 = min(inside_parameters), max(inside_parameters)
        return planar * (t1 - t0), (t0 + t1) / 2.0, t1

    def _obstacle_intersection(
        self, obstacle: Obstacle, source: Node, target: Node
    ) -> tuple[float, float | None, float | None]:
        if obstacle.shape == "brush" and obstacle.points:
            return self._segment_brush_intersection(
                source.x, source.y, target.x, target.y, obstacle.points, obstacle.brush_radius_m
            )
        if obstacle.shape == "polygon" and len(obstacle.points) >= 3:
            polygon = self._obstacle_polygons[id(obstacle)]
            return self._segment_polygon_intersection(source.x, source.y, target.x, target.y, polygon)
        if obstacle.kind == "Mountain":
            x_min, y_min, x_max, y_max = self._obstacle_bounds[id(obstacle)]
            triangle = [((x_min + x_max) / 2.0, y_min), (x_max, y_max), (x_min, y_max)]
            return self._segment_polygon_intersection(source.x, source.y, target.x, target.y, triangle)
        return self._segment_rect_intersection(
            source.x,
            source.y,
            target.x,
            target.y,
            self._obstacle_bounds[id(obstacle)],
        )

    def _obstacle_effects(
        self,
        source: Node,
        target: Node,
        obstacle_candidates: list[Obstacle] | None = None,
    ) -> tuple[float, list[str], str]:
        total = 0.0
        hit_names: list[str] = []
        planar_distance = max(1.0, math.hypot(target.x - source.x, target.y - source.y))
        wavelength = self.SPEED_OF_LIGHT / (source.radio.frequency_mhz * 1_000_000.0)
        candidates = (
            obstacle_candidates
            if obstacle_candidates is not None
            else self._candidate_obstacles(source, target)
        )
        for obstacle in candidates:
            if not obstacle.enabled:
                continue
            inside_length, midpoint_t, exit_t = self._obstacle_intersection(obstacle, source, target)
            if midpoint_t is None:
                continue
            los_z = source.antenna_z + (target.antenna_z - source.antenna_z) * midpoint_t
            top_z = obstacle.base_elevation_m + obstacle.height_m
            if los_z > top_z:
                d1 = planar_distance * midpoint_t
                d2 = planar_distance - d1
                fresnel = math.sqrt(max(0.0, wavelength * d1 * d2 / planar_distance))
                clearance = los_z - top_z
                if clearance >= 0.6 * fresnel:
                    continue
                height_factor = max(0.1, 1.0 - clearance / max(0.1, 0.6 * fresnel))
            else:
                height_factor = 1.0
            loss = obstacle.attenuation_db * height_factor
            loss += obstacle.loss_per_100m_db * (inside_length / 100.0) * height_factor
            total += loss
            hit_names.append(f"{obstacle.name} ({loss:.1f} dB)")
            if obstacle.behavior == "BLOCK":
                return total, hit_names, f"{obstacle.name} blocks line of sight"
            if obstacle.behavior == "LIMIT_AFTER" and exit_t is not None and obstacle.max_range_beyond_m > 0:
                distance_after = planar_distance * (1.0 - exit_t)
                if distance_after > obstacle.max_range_beyond_m:
                    return (
                        total,
                        hit_names,
                        f"{obstacle.name} limits travel to {obstacle.max_range_beyond_m / 1609.344:.2f} mi beyond it",
                    )
        return total, hit_names, ""

    def _terrain_effects(self, source: Node, target: Node) -> tuple[float, str]:
        environment = self.scenario.environment
        if not environment.terrain_enabled or not environment.terrain_values:
            return 0.0, ""
        planar_distance = math.hypot(target.x - source.x, target.y - source.y)
        if planar_distance < 2.0:
            return 0.0, ""
        wavelength = self.SPEED_OF_LIGHT / (source.radio.frequency_mhz * 1_000_000.0)
        samples = max(12, min(96, math.ceil(planar_distance / 100.0)))
        worst_intrusion = 0.0
        source_grid_ground = environment.terrain_elevation(source.x, source.y)
        target_grid_ground = environment.terrain_elevation(target.x, target.y)
        source_ground_offset = (
            source.elevation_m - source_grid_ground
            if source_grid_ground is not None and not source.elevation_override
            else 0.0
        )
        target_ground_offset = (
            target.elevation_m - target_grid_ground
            if target_grid_ground is not None and not target.elevation_override
            else 0.0
        )
        for index in range(1, samples):
            t = index / samples
            x = source.x + (target.x - source.x) * t
            y = source.y + (target.y - source.y) * t
            terrain_z = environment.terrain_elevation(x, y)
            if terrain_z is None:
                continue
            # Exact viewport DEM samples can differ slightly from the bounded RF
            # terrain grid. Blend that endpoint offset through the profile so an
            # automatically grounded antenna is never placed below its own terrain.
            terrain_z += source_ground_offset * (1.0 - t) + target_ground_offset * t
            los_z = source.antenna_z + (target.antenna_z - source.antenna_z) * t
            if terrain_z >= los_z:
                return 0.0, f"Topography blocks line of sight at {terrain_z:.0f} m elevation"
            d1 = planar_distance * t
            d2 = planar_distance - d1
            fresnel = math.sqrt(max(0.0, wavelength * d1 * d2 / planar_distance))
            required_clearance = 0.6 * fresnel
            if required_clearance > 0:
                intrusion = max(0.0, 1.0 - (los_z - terrain_z) / required_clearance)
                worst_intrusion = max(worst_intrusion, intrusion)
        return 24.0 * worst_intrusion, ""

    def obstacle_loss(self, source: Node, target: Node) -> tuple[float, list[str]]:
        loss, names, _blocked = self._obstacle_effects(source, target)
        return loss, names

    @staticmethod
    def radios_compatible(source: Node, target: Node) -> tuple[bool, str]:
        if not source.online or not target.online:
            return False, "offline"
        a, b = source.radio, target.radio
        freq_tolerance = min(a.bandwidth_khz, b.bandwidth_khz) / 2000.0 * 0.15
        if abs(a.frequency_mhz - b.frequency_mhz) > max(0.002, freq_tolerance):
            return False, "frequency mismatch"
        if abs(a.bandwidth_khz - b.bandwidth_khz) > 0.2:
            return False, "bandwidth mismatch"
        if a.spreading_factor != b.spreading_factor:
            return False, "spreading factor mismatch"
        if a.coding_rate != b.coding_rate:
            return False, "coding rate mismatch"
        return True, "compatible"

    def link(
        self,
        source: Node,
        target: Node,
        sample_shadowing: bool = False,
        rng: random.Random | None = None,
        obstacle_candidates: list[Obstacle] | None = None,
    ) -> LinkResult:
        compatible, reason = self.radios_compatible(source, target)
        horizontal = math.hypot(target.x - source.x, target.y - source.y)
        distance = max(1.0, math.hypot(horizontal, target.antenna_z - source.antenna_z))
        frequency_hz = max(1.0, source.radio.frequency_mhz * 1_000_000.0)
        fspl_1m = 20.0 * math.log10(4.0 * math.pi * frequency_hz / self.SPEED_OF_LIGHT)
        path_loss = fspl_1m + 10.0 * self.scenario.environment.path_loss_exponent * math.log10(distance)
        obstacle_loss, obstacles, blocked_reason = self._obstacle_effects(
            source,
            target,
            obstacle_candidates,
        )
        terrain_loss, terrain_blocked_reason = self._terrain_effects(source, target)
        obstacle_loss += terrain_loss
        if terrain_loss > 0:
            obstacles.append(f"Topography/Fresnel ({terrain_loss:.1f} dB)")
        blocked_reason = blocked_reason or terrain_blocked_reason
        shadow = 0.0
        if sample_shadowing and self.scenario.environment.stochastic:
            shadow = (rng or random).gauss(0.0, self.scenario.environment.shadowing_sigma_db)
        rssi = (
            source.tx_power_dbm
            + source.antenna_gain_dbi
            + target.antenna_gain_dbi
            - source.cable_loss_db
            - target.cable_loss_db
            - path_loss
            - obstacle_loss
            - self.scenario.environment.weather_loss_db
            + shadow
        )
        noise = self.noise_floor(target)
        snr = rssi - noise
        required = REQUIRED_SNR.get(target.radio.spreading_factor, -7.5)
        margin = snr - required
        probability = 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, margin)) / 2.0))
        if blocked_reason:
            compatible = False
            reason = blocked_reason
            probability = 0.0
        elif not compatible:
            probability = 0.0
        return LinkResult(
            source.id,
            target.id,
            distance,
            rssi,
            snr,
            required,
            margin,
            probability,
            obstacle_loss,
            path_loss,
            compatible,
            reason,
            obstacles,
        )

    _MISMATCH_REASONS = frozenset(
        {
            "frequency mismatch",
            "bandwidth mismatch",
            "spreading factor mismatch",
            "coding rate mismatch",
            "offline",
        }
    )

    def _ray_probe(self, source: Node, reference: Node) -> Node:
        """A stand-in receiver used to sample how far a beacon's signal travels."""
        probe = Node(
            id=f"beacon-probe-{source.id}",
            name="Beacon probe",
            x=source.x,
            y=source.y,
            elevation_m=source.elevation_m,
            antenna_height_m=reference.antenna_height_m,
            antenna_gain_dbi=reference.antenna_gain_dbi,
            cable_loss_db=reference.cable_loss_db,
            noise_figure_db=reference.noise_figure_db,
            channel=source.channel,
        )
        probe.radio = type(source.radio)(**vars(source.radio))
        return probe

    def _ray_blocking_obstacles(
        self, source: Node, target: Node, candidates: list[Obstacle]
    ) -> list[str]:
        """Obstacle ids whose physical volume intrudes on this beam's line of sight."""
        hits: list[str] = []
        for obstacle in candidates:
            if not obstacle.enabled:
                continue
            _inside, midpoint_t, _exit_t = self._obstacle_intersection(obstacle, source, target)
            if midpoint_t is None:
                continue
            los_z = source.antenna_z + (target.antenna_z - source.antenna_z) * midpoint_t
            top_z = obstacle.base_elevation_m + obstacle.height_m
            # Count it as a culprit when the beam grazes into the Fresnel zone,
            # not only on a dead-centre hit, so weakened edges light up too.
            if los_z <= top_z + 1.0:
                hits.append(obstacle.id)
        return hits

    def beacon_profile(
        self,
        source: Node,
        angular_samples: int = 120,
        weaken_ratio: float = 0.85,
        weaken_loss_db: float = 4.0,
        max_range_m: float | None = None,
        binary_iterations: int = 14,
    ) -> "BeaconProfile":
        """Radially probe a beacon: where its signal reaches, fades, or is blocked.

        ``max_range_m`` caps how far each beam is traced.  The unobstructed link
        budget can be 100+ km, which makes every ray scan a huge slice of the
        map; callers pass a viewport-sized cap so the sweep stays cheap.
        """
        environment = self.scenario.environment
        compatible_receivers = [
            node
            for node in self.scenario.nodes
            if node.id != source.id and self.radios_compatible(source, node)[0]
        ]
        reference = (
            compatible_receivers[len(compatible_receivers) // 2]
            if compatible_receivers
            else source
        )
        probe = self._ray_probe(source, reference)
        clear_range = self.unobstructed_range_m(source, probe)
        maximum_range = clear_range * 1.08
        if max_range_m is not None:
            maximum_range = min(maximum_range, max(500.0, float(max_range_m)))
        # Everything past the traced range is unknown, so judge "how far it should
        # have reached" against the capped range too, not the full link budget.
        clear_reference = max(1.0, min(clear_range, maximum_range))

        def position(dx: float, dy: float, distance: float) -> None:
            probe.x = source.x + dx * distance
            probe.y = source.y + dy * distance
            elevation = environment.terrain_elevation(probe.x, probe.y)
            probe.elevation_m = source.elevation_m if elevation is None else elevation

        def sample(dx: float, dy: float, distance: float) -> LinkResult:
            position(dx, dy, distance)
            return self.link(source, probe, obstacle_candidates=ray_obstacles)

        rays: list[BeaconRay] = []
        blocking: list[str] = []
        weakening: list[str] = []
        samples = max(8, angular_samples)
        for index in range(samples):
            angle = math.tau * index / samples
            dx, dy = math.cos(angle), math.sin(angle)
            probe.x = source.x + dx * maximum_range
            probe.y = source.y + dy * maximum_range
            ray_obstacles = self._candidate_obstacles(source, probe)

            far_link = sample(dx, dy, maximum_range)
            if far_link.compatible and far_link.margin_db >= 0:
                reach = maximum_range
                hard_blocked = False
            else:
                low, high = 0.0, maximum_range
                failed_link = far_link
                for _iteration in range(binary_iterations):
                    midpoint = (low + high) / 2.0
                    midpoint_link = sample(dx, dy, max(1.0, midpoint))
                    if midpoint_link.compatible and midpoint_link.margin_db >= 0:
                        low = midpoint
                    else:
                        high = midpoint
                        failed_link = midpoint_link
                reach = low
                hard_blocked = (
                    not failed_link.compatible
                    and failed_link.reason not in self._MISMATCH_REASONS
                )

            # Attribute culprit obstacles only up to where the signal actually
            # reached, plus a tiny fixed margin so the blocker sitting right at the
            # edge is caught -- never buildings sitting out beyond the coverage.
            attribution = min(maximum_range, reach + 60.0)
            position(dx, dy, attribution)
            obstacle_ids = self._ray_blocking_obstacles(source, probe, ray_obstacles)

            if hard_blocked:
                kind = "blocked"
                blocking.extend(obstacle_ids)
            else:
                # A beam that falls well short of the open-air range was cut down by
                # something 3-D in the way -- a building OR the terrain/a mountain --
                # so it counts as weakened even when no building volume is hit.
                weakened = reach < weaken_ratio * clear_reference
                # Otherwise pay for one extra attenuation probe only when the beam
                # actually grazes a building (e.g. forest that thins but still reaches).
                if not weakened and obstacle_ids:
                    mid_link = sample(dx, dy, max(1.0, min(reach, clear_reference) * 0.7))
                    weakened = mid_link.obstacle_loss_db >= weaken_loss_db
                kind = "weakened" if weakened else "clear"
                if weakened:
                    weakening.extend(obstacle_ids)
            rays.append(BeaconRay(angle, reach, clear_reference, kind, obstacle_ids))

        return BeaconProfile(
            source.id,
            source.x,
            source.y,
            rays,
            list(dict.fromkeys(blocking)),
            list(dict.fromkeys(weakening)),
            maximum_range,
        )


def dm_route_key(source_id: str, destination_id: str) -> str:
    return f"{source_id}>{destination_id}"


class SimulationEngine:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.model = PropagationModel(scenario)
        self.rng = random.Random(scenario.environment.seed)

    @staticmethod
    def _can_cancel(role: str) -> bool:
        return role not in {"ROUTER", "ROUTER_CLIENT", "ROUTER_LATE", "REPEATER"}

    def _can_relay(self, node: Node, packet: PacketConfig, decoded: bool) -> tuple[bool, str]:
        if node.role == "CLIENT_MUTE":
            return False, "CLIENT_MUTE does not rebroadcast"
        mode = node.rebroadcast_mode
        if mode == "NONE":
            return False, "rebroadcast mode NONE"
        if mode in {"LOCAL_ONLY", "KNOWN_ONLY"} and not decoded:
            return False, f"{mode} rejects foreign/opaque channel"
        if mode == "CORE_PORTNUMS_ONLY" and packet.port not in CORE_PORTS:
            return False, "non-core port filtered"
        return True, "eligible"

    def _relay_delay_ms(self, node: Node, snr: float) -> float:
        sf = node.radio.spreading_factor
        bw = max(1.0, node.radio.bandwidth_khz)
        symbol_ms = (2**sf) / bw
        slot_ms = max(1.0, 2.5 * symbol_ms + 7.6)
        cw = round(3 + (max(-20.0, min(10.0, snr)) + 20.0) / 30.0 * 5)
        if node.role in {"ROUTER", "ROUTER_CLIENT", "REPEATER"}:
            return self.rng.randrange(max(1, 2 * cw)) * slot_ms
        base = 16 * slot_ms
        delay = base + self.rng.randrange(max(1, 2**cw)) * slot_ms
        if node.role in {"ROUTER_LATE", "CLIENT_BASE"}:
            delay += (2**cw) * slot_ms
        return delay

    @staticmethod
    def _path_from_result(result: SimulationResult, destination_id: str) -> list[str]:
        path: list[str] = []
        seen: set[str] = set()
        current = destination_id
        while current and current not in seen:
            seen.add(current)
            path.append(current)
            current = str(result.reached.get(current, {}).get("via", ""))
        path.reverse()
        return path

    def _route_acknowledged(self, route: list[str], nodes: dict[str, Node]) -> bool:
        for index in range(len(route) - 1, 0, -1):
            sender = nodes.get(route[index])
            receiver = nodes.get(route[index - 1])
            if sender is None or receiver is None or not sender.online or not receiver.online:
                return False
            link = self.model.link(sender, receiver, sample_shadowing=True, rng=self.rng)
            if not link.compatible or link.margin_db < 0 or receiver.channel != sender.channel:
                return False
            if self.scenario.environment.stochastic and self.rng.random() > link.probability:
                return False
        return True

    def _run_learned_route(
        self,
        packet: PacketConfig,
        route: list[str],
        nodes: dict[str, Node],
    ) -> SimulationResult:
        result = SimulationResult(
            routing_mode="DM_LEARNED",
            route_key=dm_route_key(packet.source_id, packet.destination_id),
            learned_route=list(route),
        )
        source = nodes[packet.source_id]
        result.reached[source.id] = {"time_ms": 0.0, "hop": 0, "decoded": True, "via": ""}
        elapsed = 0.0

        for edge_index in range(len(route) - 1):
            transmitter = nodes.get(route[edge_index])
            receiver = nodes.get(route[edge_index + 1])
            hop = edge_index + 1
            if (
                transmitter is None
                or receiver is None
                or not transmitter.online
                or not receiver.online
                or edge_index > packet.hop_limit
            ):
                result.events.append(
                    SimEvent(elapsed, "ROUTE_FAILED", route[edge_index], hop=hop, detail="learned route is unavailable")
                )
                return result

            if edge_index > 0:
                decoded_at_relay = transmitter.channel == packet.channel
                can_relay, reason = self._can_relay(transmitter, packet, decoded_at_relay)
                if not can_relay:
                    result.events.append(
                        SimEvent(elapsed, "ROUTE_FAILED", transmitter.id, hop=hop, detail=reason)
                    )
                    return result

            airtime = self.model.airtime_ms(transmitter, packet.payload_bytes)
            result.events.append(
                SimEvent(
                    elapsed,
                    "TX",
                    transmitter.id,
                    route[edge_index - 1] if edge_index else "",
                    edge_index,
                    detail="learned next-hop transmission",
                    airtime_ms=airtime,
                )
            )
            result.transmissions += 1
            result.total_airtime_ms += airtime
            link = self.model.link(transmitter, receiver, sample_shadowing=True, rng=self.rng)
            result.links.append(link)
            arrival = elapsed + airtime
            success = link.compatible and link.margin_db >= 0
            if self.scenario.environment.stochastic:
                success = success and self.rng.random() <= link.probability
            decoded = receiver.channel == packet.channel
            if not success or not decoded:
                result.dropped += 1
                result.events.append(
                    SimEvent(
                        arrival,
                        "DROP",
                        receiver.id,
                        transmitter.id,
                        hop,
                        link.rssi_dbm,
                        link.snr_db,
                        link.margin_db,
                        link.reason if not success else "destination or relay cannot decode packet channel",
                        decoded,
                    )
                )
                result.duration_ms = arrival
                return result

            result.receptions += 1
            result.decoded += 1
            result.events.append(
                SimEvent(
                    arrival,
                    "RX",
                    receiver.id,
                    transmitter.id,
                    hop,
                    link.rssi_dbm,
                    link.snr_db,
                    link.margin_db,
                    "learned next hop",
                    True,
                )
            )
            result.reached[receiver.id] = {
                "time_ms": arrival,
                "hop": hop,
                "decoded": True,
                "via": transmitter.id,
                "rssi_dbm": link.rssi_dbm,
                "snr_db": link.snr_db,
                "margin_db": link.margin_db,
            }
            result.max_distance_m = max(
                result.max_distance_m,
                math.hypot(receiver.x - source.x, receiver.y - source.y),
            )
            result.duration_ms = arrival
            if receiver.id == packet.destination_id:
                result.acknowledged = not packet.want_ack or self._route_acknowledged(route, nodes)
                if packet.want_ack:
                    result.events.append(
                        SimEvent(
                            arrival,
                            "ACK" if result.acknowledged else "ACK_FAILED",
                            packet.source_id,
                            packet.destination_id,
                            hop,
                            detail="end-to-end delivery confirmed" if result.acknowledged else "return ACK path failed",
                        )
                    )
                return result
            elapsed = arrival + self._relay_delay_ms(receiver, link.snr_db)

        return result

    def run(self, packet: PacketConfig | None = None) -> SimulationResult:
        packet = packet or self.scenario.packet
        result = SimulationResult()
        nodes = {node.id: node for node in self.scenario.nodes}
        source = nodes.get(packet.source_id)
        if not source or not source.online:
            result.events.append(SimEvent(0, "DROP", packet.source_id, detail="Source is missing or offline"))
            result.dropped = 1
            return result

        packet.hop_limit = max(0, min(7, int(packet.hop_limit)))
        destination = packet.destination_id
        broadcast = destination == "BROADCAST"
        route_key = "" if broadcast else dm_route_key(source.id, destination)
        cached_route = self.scenario.learned_routes.get(route_key, [])
        valid_route_shape = (
            len(cached_route) >= 2
            and cached_route[0] == source.id
            and cached_route[-1] == destination
            and len(set(cached_route)) == len(cached_route)
        )
        if valid_route_shape:
            directed = self._run_learned_route(packet, cached_route, nodes)
            if destination in directed.reached and (not packet.want_ack or directed.acknowledged):
                return directed
            self.scenario.learned_routes.pop(route_key, None)
            result = SimulationResult(
                routing_mode="DM_FALLBACK_FLOOD",
                route_key=route_key,
                invalidated_route_key=route_key,
            )
            result.events.append(
                SimEvent(0.0, "ROUTE_FALLBACK", source.id, destination, detail="learned path failed; using managed flood")
            )
        elif cached_route:
            self.scenario.learned_routes.pop(route_key, None)
            result.routing_mode = "DM_FALLBACK_FLOOD"
            result.route_key = route_key
            result.invalidated_route_key = route_key
            result.events.append(
                SimEvent(0.0, "ROUTE_FALLBACK", source.id, destination, detail="stored route was invalid; using managed flood")
            )
        elif not broadcast:
            result.routing_mode = "DM_DISCOVERY_FLOOD"
            result.route_key = route_key
        result.reached[source.id] = {"time_ms": 0.0, "hop": 0, "decoded": True, "via": ""}
        seen: dict[str, float] = {source.id: 0.0}
        pending_tokens: dict[str, dict[str, Any]] = {}
        busy: dict[str, list[tuple[float, float, float, str]]] = {node.id: [] for node in self.scenario.nodes}
        queue: list[tuple[float, int, str, str, int, int, dict[str, Any] | None]] = []
        sequence = 0

        def push(
            time_ms: float,
            tx_id: str,
            via_id: str,
            hops_left: int,
            hop: int,
            token: dict[str, Any] | None = None,
        ) -> None:
            nonlocal sequence
            sequence += 1
            heapq.heappush(queue, (time_ms, sequence, tx_id, via_id, hops_left, hop, token))

        push(0.0, source.id, "", packet.hop_limit, 0)

        while queue and len(result.events) < 20000:
            tx_time, _, tx_id, via_id, hops_left, hop, token = heapq.heappop(queue)
            if token is not None and token.get("cancelled"):
                result.events.append(
                    SimEvent(tx_time, "CANCEL", tx_id, via_id, hop, detail="duplicate relay heard before transmit")
                )
                continue
            tx = nodes.get(tx_id)
            if not tx or not tx.online:
                continue
            airtime = self.model.airtime_ms(tx, packet.payload_bytes)
            result.events.append(
                SimEvent(
                    tx_time,
                    "TX",
                    tx.id,
                    via_id,
                    hop,
                    detail=f"{hops_left} hops remaining",
                    airtime_ms=airtime,
                )
            )
            result.transmissions += 1
            result.total_airtime_ms += airtime
            result.duration_ms = max(result.duration_ms, tx_time + airtime)

            for rx in self.scenario.nodes:
                if rx.id == tx.id or not rx.online:
                    continue
                link = self.model.link(tx, rx, sample_shadowing=True, rng=self.rng)
                result.links.append(link)
                if not link.compatible:
                    result.events.append(
                        SimEvent(
                            tx_time,
                            "INCOMPATIBLE",
                            rx.id,
                            tx.id,
                            hop + 1,
                            detail=link.reason,
                            decoded=False,
                        )
                    )
                    continue

                arrival_start = tx_time
                arrival_end = tx_time + airtime
                collision = False
                for old_start, old_end, old_rssi, old_tx in busy[rx.id]:
                    if arrival_start < old_end and arrival_end > old_start and old_tx != tx.id:
                        if abs(old_rssi - link.rssi_dbm) < self.scenario.environment.capture_threshold_db:
                            collision = True
                            break
                        if old_rssi > link.rssi_dbm:
                            collision = True
                            break
                busy[rx.id].append((arrival_start, arrival_end, link.rssi_dbm, tx.id))
                busy[rx.id] = [entry for entry in busy[rx.id] if entry[1] >= tx_time - 1]

                if collision:
                    result.collisions += 1
                    result.events.append(
                        SimEvent(
                            arrival_end,
                            "COLLISION",
                            rx.id,
                            tx.id,
                            hop + 1,
                            link.rssi_dbm,
                            link.snr_db,
                            link.margin_db,
                            "overlapping LoRa transmission",
                            False,
                        )
                    )
                    continue

                success = link.margin_db >= 0
                if self.scenario.environment.stochastic:
                    success = self.rng.random() <= link.probability
                if not success:
                    result.dropped += 1
                    result.events.append(
                        SimEvent(
                            arrival_end,
                            "DROP",
                            rx.id,
                            tx.id,
                            hop + 1,
                            link.rssi_dbm,
                            link.snr_db,
                            link.margin_db,
                            "below receive threshold",
                            False,
                        )
                    )
                    continue

                decoded = rx.channel == packet.channel
                result.receptions += 1
                if decoded:
                    result.decoded += 1
                duplicate = rx.id in seen
                result.events.append(
                    SimEvent(
                        arrival_end,
                        "DUPLICATE" if duplicate else ("RX" if decoded else "OPAQUE"),
                        rx.id,
                        tx.id,
                        hop + 1,
                        link.rssi_dbm,
                        link.snr_db,
                        link.margin_db,
                        "decoded" if decoded else "channel hash/PSK does not match",
                        decoded,
                    )
                )

                if duplicate:
                    pending = pending_tokens.get(rx.id)
                    if pending and self._can_cancel(rx.role):
                        pending["cancelled"] = True
                    continue

                seen[rx.id] = arrival_end
                result.reached[rx.id] = {
                    "time_ms": arrival_end,
                    "hop": hop + 1,
                    "decoded": decoded,
                    "via": tx.id,
                    "rssi_dbm": link.rssi_dbm,
                    "snr_db": link.snr_db,
                    "margin_db": link.margin_db,
                }
                result.max_distance_m = max(
                    result.max_distance_m,
                    math.hypot(rx.x - source.x, rx.y - source.y),
                )

                # The displayed hop limit is the furthest receiving hop.  A
                # packet set to 3 therefore reaches through H3, never H4.
                if (not broadcast and rx.id == destination) or hop + 1 >= packet.hop_limit:
                    continue
                can_relay, reason = self._can_relay(rx, packet, decoded)
                if not can_relay:
                    result.events.append(SimEvent(arrival_end, "NO_RELAY", rx.id, tx.id, hop + 1, detail=reason, decoded=decoded))
                    continue
                delay = self._relay_delay_ms(rx, link.snr_db)
                relay_token: dict[str, Any] = {"cancelled": False}
                pending_tokens[rx.id] = relay_token
                push(arrival_end + delay, rx.id, tx.id, hops_left - 1, hop + 1, relay_token)

        if not broadcast:
            destination_info = result.reached.get(destination)
            if destination_info and destination_info.get("decoded") and packet.want_ack:
                route = self._path_from_result(result, destination)
                if (
                    len(route) >= 2
                    and route[0] == source.id
                    and route[-1] == destination
                    and self._route_acknowledged(route, nodes)
                ):
                    result.acknowledged = True
                    result.learned_route = route
                    self.scenario.learned_routes[route_key] = route
                    result.events.append(
                        SimEvent(
                            result.duration_ms,
                            "ROUTE_LEARNED",
                            destination,
                            source.id,
                            len(route) - 1,
                            detail="ACK confirmed next-hop path",
                        )
                    )
                else:
                    result.events.append(
                        SimEvent(
                            result.duration_ms,
                            "ACK_FAILED",
                            source.id,
                            destination,
                            detail="destination was reached but the return ACK path failed",
                        )
                    )
        result.events.sort(key=lambda event: (event.time_ms, event.kind))
        return result


def scenario_from_file(path: str) -> Scenario:
    with open(path, "r", encoding="utf-8") as handle:
        return Scenario.from_json(handle.read())


def scenario_to_file(path: str, scenario: Scenario) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(scenario.to_json())
