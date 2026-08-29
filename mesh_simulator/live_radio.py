from __future__ import annotations

import copy
import math
import queue
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pubsub import pub
from serial.tools import list_ports

if TYPE_CHECKING:
    from meshtastic.serial_interface import SerialInterface


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str
    hardware_id: str

    @property
    def label(self) -> str:
        description = self.description.strip()
        return f"{self.device} — {description}" if description else self.device


@dataclass(frozen=True)
class LiveNode:
    node_num: int
    name: str
    short_name: str
    role: str
    hardware_model: str
    latitude: float | None
    longitude: float | None
    # MSL is directly compatible with the simulator's terrain elevations.
    altitude_m: float | None
    altitude_hae_m: float | None
    geoidal_separation_m: float | None
    altitude_source: str
    altitude_accuracy_m: float | None
    precision_bits: int | None
    hops_away: int | None
    snr_db: float | None
    last_heard: int | None
    favorite: bool

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _port_number(device: str) -> int:
    match = re.search(r"(\d+)$", device)
    return int(match.group(1)) if match else 1_000_000


def _is_bluetooth_port(port: SerialPort) -> bool:
    text = f"{port.description} {port.hardware_id}".lower()
    return "bluetooth" in text or "bthenum" in text


def list_serial_ports() -> list[SerialPort]:
    ports = [
        SerialPort(
            device=str(port.device),
            description=str(port.description or ""),
            hardware_id=str(port.hwid or ""),
        )
        for port in list_ports.comports()
    ]
    ports = [port for port in ports if not _is_bluetooth_port(port)]

    def priority(port: SerialPort) -> tuple[int, int, str]:
        text = f"{port.description} {port.hardware_id}".lower()
        likely_usb_radio = "usb" in text
        return (0 if likely_usb_radio else 1, _port_number(port.device), port.device)

    return sorted(ports, key=priority)


def _number(value: Any, kind: type[int] | type[float]) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def _position_value(position: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in position:
            return position[name]
    return None


def _altitude_accuracy(position: dict[str, Any]) -> float | None:
    """Estimate vertical uncertainty from Meshtastic's 1/100 DOP and mm accuracy."""
    dop = _number(_position_value(position, "VDOP", "vdop", "PDOP", "pdop"), float)
    if dop is None or not math.isfinite(dop) or dop <= 0:
        return None
    base_accuracy_mm = _number(
        _position_value(position, "gpsAccuracy", "gps_accuracy"),
        float,
    )
    base_accuracy_m = (
        3.0
        if base_accuracy_mm is None or not math.isfinite(base_accuracy_mm) or base_accuracy_mm <= 0
        else base_accuracy_mm / 1000.0
    )
    return base_accuracy_m * dop / 100.0


def parse_live_node(raw: dict[str, Any], fallback_num: int | None = None) -> LiveNode | None:
    node_num = _number(raw.get("num", fallback_num), int)
    if node_num is None:
        return None
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    position = raw.get("position") if isinstance(raw.get("position"), dict) else {}

    latitude = _number(position.get("latitude"), float)
    longitude = _number(position.get("longitude"), float)
    if latitude is None and "latitudeI" in position:
        latitude_i = _number(position.get("latitudeI"), int)
        latitude = None if latitude_i is None else latitude_i * 1e-7
    if longitude is None and "longitudeI" in position:
        longitude_i = _number(position.get("longitudeI"), int)
        longitude = None if longitude_i is None else longitude_i * 1e-7
    if latitude is not None and not -90.0 <= latitude <= 90.0:
        latitude = None
    if longitude is not None and not -180.0 <= longitude <= 180.0:
        longitude = None
    if latitude is None or longitude is None:
        latitude = longitude = None

    presumptive_id = f"!{int(node_num):08x}"
    name = str(user.get("longName") or user.get("shortName") or presumptive_id)
    short_name = str(user.get("shortName") or presumptive_id[-4:])
    role = str(user.get("role") or "CLIENT").upper()
    hardware_model = str(user.get("hwModel") or "UNSET")
    altitude_m = _number(position.get("altitude"), float) if "altitude" in position else None
    altitude_hae_m = _number(
        _position_value(position, "altitudeHae", "altitude_hae"),
        float,
    )
    geoidal_separation_m = _number(
        _position_value(
            position,
            "altitudeGeoidalSeparation",
            "altitude_geoidal_separation",
        ),
        float,
    )
    altitude_m = altitude_m if altitude_m is not None and math.isfinite(altitude_m) else None
    altitude_hae_m = (
        altitude_hae_m
        if altitude_hae_m is not None and math.isfinite(altitude_hae_m)
        else None
    )
    geoidal_separation_m = (
        geoidal_separation_m
        if geoidal_separation_m is not None and math.isfinite(geoidal_separation_m)
        else None
    )
    # Firmware defines HAE = MSL + geoidal separation. Convert only when both
    # values were transmitted; silently treating HAE as MSL can be tens of
    # meters wrong.
    if altitude_m is None and altitude_hae_m is not None and geoidal_separation_m is not None:
        altitude_m = altitude_hae_m - geoidal_separation_m
    altitude_source = str(
        _position_value(position, "altitudeSource", "altitude_source")
        or _position_value(position, "locationSource", "location_source")
        or "ALT_UNSET"
    )
    return LiveNode(
        node_num=int(node_num),
        name=name,
        short_name=short_name,
        role=role,
        hardware_model=hardware_model,
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude_m,
        altitude_hae_m=altitude_hae_m,
        geoidal_separation_m=geoidal_separation_m,
        altitude_source=altitude_source,
        altitude_accuracy_m=_altitude_accuracy(position),
        precision_bits=_number(position.get("precisionBits"), int),
        hops_away=_number(raw.get("hopsAway"), int),
        snr_db=_number(raw.get("snr"), float),
        last_heard=_number(raw.get("lastHeard"), int),
        favorite=bool(raw.get("isFavorite", False)),
    )


class LiveRadioClient:
    """Threaded, read-only adapter around the official Meshtastic serial API."""

    def __init__(self) -> None:
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._interface: SerialInterface | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._subscribed = False
        self.port = ""
        self.connecting = False

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._interface is not None

    def connect(self, port: str, timeout: int = 30) -> None:
        with self._lock:
            if self.connecting or self._interface is not None:
                raise RuntimeError("A radio connection is already active")
            self._generation += 1
            generation = self._generation
            self.connecting = True
            self.port = port
        self._subscribe()

        def worker() -> None:
            # Meshtastic imports its protobuf stack, which is relatively heavy.
            # Defer it until the COM-radio feature is actually used so map and
            # survey-only sessions do not pay that startup cost.
            from meshtastic.serial_interface import SerialInterface

            interface: SerialInterface | None = None
            try:
                interface = SerialInterface(devPath=port, timeout=timeout)
                with self._lock:
                    if generation != self._generation:
                        interface.close()
                        return
                    self._interface = interface
                    self.connecting = False
                raw_nodes = copy.deepcopy(interface.nodesByNum or {})
                nodes = [
                    parsed
                    for number, raw in raw_nodes.items()
                    if (parsed := parse_live_node(raw, int(number))) is not None
                ]
                self.events.put(("connected", {"port": port, "nodes": nodes}))
            except Exception as error:
                if interface is not None:
                    try:
                        interface.close()
                    except Exception:
                        pass
                with self._lock:
                    if generation == self._generation:
                        self.connecting = False
                self._unsubscribe()
                self.events.put(("error", {"port": port, "error": error}))

        threading.Thread(target=worker, name="MeshtasticConnect", daemon=True).start()

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            interface = self._interface
            self._interface = None
            self.connecting = False
            port = self.port
        self._unsubscribe()

        def worker() -> None:
            if interface is not None:
                try:
                    interface.close()
                except Exception as error:
                    self.events.put(("close_error", {"port": port, "error": error}))
            self.events.put(("disconnected", {"port": port}))

        threading.Thread(target=worker, name="MeshtasticDisconnect", daemon=True).start()

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        for listener, topic in (
            (self._on_node_updated, "meshtastic.node.updated"),
            (self._on_receive, "meshtastic.receive"),
            (self._on_connection_lost, "meshtastic.connection.lost"),
        ):
            try:
                pub.unsubscribe(listener, topic)
            except Exception:
                pass
        self._subscribed = False

    def _accept_interface(self, interface: Any) -> bool:
        with self._lock:
            return self._interface is None or interface is self._interface

    def _on_node_updated(self, node: dict[str, Any], interface: Any, **_kwargs: Any) -> None:
        if not self._accept_interface(interface):
            return
        parsed = parse_live_node(copy.deepcopy(node))
        if parsed is not None:
            self.events.put(("node", parsed))

    def _on_receive(self, packet: dict[str, Any], interface: Any, **_kwargs: Any) -> None:
        if not self._accept_interface(interface):
            return
        node_num = _number(packet.get("from"), int)
        if node_num is None:
            return
        raw = (interface.nodesByNum or {}).get(int(node_num))
        if raw is None:
            return
        parsed = parse_live_node(copy.deepcopy(raw), int(node_num))
        if parsed is not None:
            self.events.put(("node", parsed))

    def _on_connection_lost(self, interface: Any, **_kwargs: Any) -> None:
        if not self._accept_interface(interface):
            return
        with self._lock:
            if interface is self._interface:
                self._interface = None
            self.connecting = False
        self._unsubscribe()
        self.events.put(("lost", {"port": self.port}))
