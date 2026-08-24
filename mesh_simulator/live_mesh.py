from __future__ import annotations

import heapq
import math
import random
import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import (
    CORE_PORTS,
    MIN_DECODE_MARGIN_DB,
    LinkResult,
    LiveMeshConfig,
    Node,
    PacketConfig,
    PropagationModel,
    Scenario,
    dm_route_key,
)


POLITE_CHANNEL_LIMIT_PERCENT = 25.0
MAX_CHANNEL_LIMIT_PERCENT = 40.0
CHANNEL_WINDOW_MS = 60_000.0
LIVE_FRAME_COUNT = 600
MAX_FRAME_TRANSMITTERS = 10
MAX_FRAME_RECEPTIONS = 14
MAX_FRAME_COLLISIONS = 10
MAX_FRAME_THROTTLES = 10
MAX_LIVE_TRANSMISSIONS = 200_000

TRAFFIC_COLORS = {
    "NODEINFO": "#38bdf8",
    "TELEMETRY": "#31d58b",
    "SENSOR": "#ffbd4a",
    "MESSAGE": "#c084fc",
    "ACK": "#facc15",
    "RESPONSE": "#fb7185",
    "NAK": "#fb7185",
    "TEST": "#ffffff",
}


@dataclass
class LiveMeshFrame:
    time_ms: float
    transmitters: list[tuple[str, str]] = field(default_factory=list)
    receptions: list[tuple[str, str, str, int]] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    throttled: list[str] = field(default_factory=list)
    transmission_count: int = 0
    reception_count: int = 0
    collision_count: int = 0
    drop_count: int = 0
    throttle_count: int = 0
    peak_channel_utilization: float = 0.0
    # Exact per-frame packet categories are kept separately from the short
    # animation lists above.  The UI caps those lists on purpose, whereas the
    # traffic chart must still account for every RF transmission and loss.
    traffic_transmissions: dict[str, int] = field(default_factory=dict)
    traffic_collisions: dict[str, int] = field(default_factory=dict)
    traffic_drops: dict[str, int] = field(default_factory=dict)
    traffic_throttles: dict[str, int] = field(default_factory=dict)


@dataclass
class LiveMeshResult:
    duration_ms: float
    frames: list[LiveMeshFrame]
    offered_packets: int = 0
    originated_packets: int = 0
    packets_heard: int = 0
    transmissions: int = 0
    receptions: int = 0
    collisions: int = 0
    dropped: int = 0
    duplicates: int = 0
    cancelled_relays: int = 0
    throttled: int = 0
    peak_channel_utilization: float = 0.0
    traffic_counts: dict[str, int] = field(default_factory=dict)
    truncated: bool = False


@dataclass
class LiveMeshTestEvent:
    """One explainable outcome from a packet injected into live traffic."""

    time_ms: float
    kind: str
    node_id: str
    peer_id: str = ""
    hop: int = 0
    rssi_dbm: float = 0.0
    snr_db: float = 0.0
    margin_db: float = 0.0
    detail: str = ""


@dataclass
class LiveMeshTestResult:
    test_id: int
    packet: PacketConfig
    time_ms: float
    status: str = "IN FLIGHT"
    reached: dict[str, dict[str, Any]] = field(default_factory=dict)
    transmissions: int = 0
    receptions: int = 0
    collisions: int = 0
    dropped: int = 0
    throttled: int = 0
    routing_mode: str = "BROADCAST_FLOOD"
    route_key: str = ""
    learned_route: list[str] = field(default_factory=list)
    invalidated_route_key: str = ""
    acknowledged: bool = False
    response_received: bool = False
    events: list[LiveMeshTestEvent] = field(default_factory=list)
    complete: bool = False


@dataclass
class _TrafficSpec:
    kind: str
    port: str
    payload_bytes: int
    interval_ms: float
    channel_limit_percent: float


@dataclass
class _PacketState:
    packet_id: int
    spec: _TrafficSpec
    packet: PacketConfig
    origin_id: str
    seen: set[str]
    pending_tokens: dict[str, "_RelayToken"] = field(default_factory=dict)
    test_id: int | None = None
    pending_events: int = 0
    directed_route: list[str] = field(default_factory=list)
    directed_index: int = 0
    arrivals: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_kind: str = ""
    reply_to_packet_id: int = 0


@dataclass
class _RelayToken:
    cancelled: bool = False


@dataclass
class _TxTask:
    packet: _PacketState
    node_id: str
    via_id: str
    hops_left: int
    hop: int
    token: _RelayToken | None = None


@dataclass
class _Reception:
    packet: _PacketState
    transmitter_id: str
    receiver_id: str
    start_ms: float
    end_ms: float
    hops_left: int
    hop: int
    link: LinkResult
    rssi_dbm: float
    snr_db: float
    collided: bool = False


class _RollingChannel:
    def __init__(self) -> None:
        self._activity: dict[str, deque[tuple[float, float]]] = {}
        self._totals: dict[str, float] = {}

    def utilization(self, node_id: str, time_ms: float) -> float:
        entries = self._activity.setdefault(node_id, deque())
        total = self._totals.get(node_id, 0.0)
        cutoff = time_ms - CHANNEL_WINDOW_MS
        while entries and entries[0][0] <= cutoff:
            _end, airtime = entries.popleft()
            total -= airtime
        self._totals[node_id] = max(0.0, total)
        return max(0.0, total) / CHANNEL_WINDOW_MS * 100.0

    def add(self, node_id: str, time_ms: float, airtime_ms: float) -> float:
        self.utilization(node_id, time_ms)
        self._activity[node_id].append((time_ms, airtime_ms))
        self._totals[node_id] = self._totals.get(node_id, 0.0) + airtime_ms
        return self._totals[node_id] / CHANNEL_WINDOW_MS * 100.0


class LiveMeshEngine:
    """Efficient, event-driven simulation of concurrent routine mesh traffic."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.model = PropagationModel(scenario)
        self.rng = random.Random(scenario.environment.seed ^ 0x4C495645)
        self.nodes = {node.id: node for node in scenario.nodes if node.online}
        self._links_by_source: dict[str, list[tuple[Node, LinkResult]]] = {}
        self._links_by_target: dict[str, dict[str, tuple[Node, LinkResult]]] = {}
        self._channel = _RollingChannel()
        self._active_receptions: dict[str, list[_Reception]] = {node_id: [] for node_id in self.nodes}
        self._events: list[tuple[float, int, str, Any]] = []
        self._sequence = 0
        self._next_packet_id = 0
        self._packet_states: list[_PacketState] = []
        self._result: LiveMeshResult | None = None
        self._runtime = False
        self._runtime_frames: dict[int, LiveMeshFrame] = {}
        self._runtime_frame_ms = 2_000.0
        self._runtime_time_ms = 0.0
        self._runtime_config: LiveMeshConfig | None = None
        self._last_state_prune_ms = 0.0
        self._tests: dict[int, LiveMeshTestResult] = {}
        self._test_request_states: dict[int, _PacketState] = {}
        self._next_test_id = 0
        self._learned_routes = {key: list(route) for key, route in scenario.learned_routes.items()}

    @staticmethod
    def _traffic_scale(profile: str) -> float:
        return 0.1 if profile == "BUSY_10X" else 1.0

    @staticmethod
    def _has_priority_channel_gate(node: Node) -> bool:
        return node.role in {"SENSOR", "ROUTER", "ROUTER_LATE"}

    @staticmethod
    def _has_unscaled_telemetry_interval(node: Node) -> bool:
        return node.role in {"SENSOR", "TRACKER", "TAK_TRACKER", "ROUTER", "ROUTER_LATE"}

    @staticmethod
    def _can_cancel(node: Node) -> bool:
        return node.role not in {"ROUTER", "ROUTER_CLIENT", "ROUTER_LATE", "REPEATER"}

    @staticmethod
    def _can_relay(node: Node, packet: PacketConfig, decoded: bool) -> bool:
        if node.role == "CLIENT_MUTE" or node.rebroadcast_mode == "NONE":
            return False
        if node.rebroadcast_mode in {"LOCAL_ONLY", "KNOWN_ONLY"} and not decoded:
            return False
        if node.rebroadcast_mode == "CORE_PORTNUMS_ONLY" and packet.port not in CORE_PORTS:
            return False
        return True

    def _congestion_scale(self, node: Node) -> float:
        count = len(self.nodes)
        if count <= 40 or self._has_unscaled_telemetry_interval(node):
            return 1.0
        factor = (2 ** max(5, min(12, node.radio.spreading_factor))) / (
            max(1.0, node.radio.bandwidth_khz) * 100.0
        )
        return 1.0 + (count - 40) * factor

    def _specs_for(self, node: Node, config: LiveMeshConfig) -> list[_TrafficSpec]:
        scale = self._traffic_scale(config.traffic_profile)
        specs: list[_TrafficSpec] = []
        if node.role != "CLIENT_HIDDEN":
            specs.append(
                _TrafficSpec(
                    "NODEINFO", "NODEINFO_APP", 48,
                    max(1.0, config.nodeinfo_interval_minutes) * 60_000.0 * scale,
                    MAX_CHANNEL_LIMIT_PERCENT,
                )
            )
            telemetry_minutes = (
                config.router_telemetry_interval_minutes
                if node.role in {"ROUTER", "ROUTER_LATE"}
                else config.telemetry_interval_minutes
            )
            telemetry_limit = (
                MAX_CHANNEL_LIMIT_PERCENT
                if self._has_priority_channel_gate(node)
                else POLITE_CHANNEL_LIMIT_PERCENT
            )
            specs.append(
                _TrafficSpec(
                    "TELEMETRY",
                    "TELEMETRY_APP",
                    40,
                    max(1.0, telemetry_minutes) * 60_000.0 * scale * self._congestion_scale(node),
                    telemetry_limit,
                )
            )
            if node.role == "SENSOR":
                specs.append(
                    _TrafficSpec(
                        "SENSOR",
                        "TELEMETRY_APP",
                        56,
                        max(1.0, config.sensor_interval_minutes) * 60_000.0 * scale,
                        MAX_CHANNEL_LIMIT_PERCENT,
                    )
                )
        return specs

    def _push(self, time_ms: float, kind: str, payload: Any) -> None:
        state: _PacketState | None = None
        if kind == "TX":
            state = payload.packet
        elif kind == "RX_END":
            state = payload.packet
        elif kind == "TEST_ORIGIN":
            state = payload
        if state is not None:
            state.pending_events += 1
        self._sequence += 1
        heapq.heappush(self._events, (time_ms, self._sequence, kind, payload))

    @staticmethod
    def _event_packet_state(kind: str, payload: Any) -> _PacketState | None:
        if kind == "TX":
            return payload.packet
        if kind == "RX_END":
            return payload.packet
        if kind == "TEST_ORIGIN":
            return payload
        return None

    def _prune_finished_runtime_packets(self, time_ms: float) -> None:
        """Release complete routine floods while retaining visible test detail."""
        if not self._runtime or time_ms - self._last_state_prune_ms < 5_000.0:
            return
        self._last_state_prune_ms = time_ms
        self._packet_states = [
            state for state in self._packet_states
            if state.test_id is not None or state.pending_events > 0
        ]

    def _frame(self, time_ms: float) -> LiveMeshFrame:
        assert self._result is not None
        if self._runtime:
            index = max(0, int(time_ms / self._runtime_frame_ms))
            frame = self._runtime_frames.get(index)
            if frame is None:
                frame = LiveMeshFrame(time_ms=index * self._runtime_frame_ms)
                self._runtime_frames[index] = frame
            return frame
        index = min(
            len(self._result.frames) - 1,
            max(0, int(time_ms / max(1.0, self._result.duration_ms) * len(self._result.frames))),
        )
        return self._result.frames[index]

    def _record_peak(self, time_ms: float, utilization: float) -> None:
        assert self._result is not None
        self._result.peak_channel_utilization = max(self._result.peak_channel_utilization, utilization)
        frame = self._frame(time_ms)
        frame.peak_channel_utilization = max(frame.peak_channel_utilization, utilization)

    def _schedule_origins(self, config: LiveMeshConfig, source_duration_ms: float) -> None:
        for node in self.nodes.values():
            for spec in self._specs_for(node, config):
                phase = self.rng.uniform(0.0, max(1.0, min(spec.interval_ms, source_duration_ms)))
                time_ms = phase
                while time_ms <= source_duration_ms:
                    self._push(time_ms, "ORIGIN", (node.id, spec, config.hop_limit, False))
                    time_ms += spec.interval_ms

            message_mean_ms = max(1.0, config.message_interval_minutes) * 60_000.0 * self._traffic_scale(config.traffic_profile)
            first_message = self.rng.uniform(0.0, min(message_mean_ms, max(1.0, source_duration_ms)))
            time_ms = first_message
            while time_ms <= source_duration_ms:
                message_spec = _TrafficSpec(
                    "MESSAGE",
                    "TEXT_MESSAGE_APP",
                    32,
                    message_mean_ms,
                    MAX_CHANNEL_LIMIT_PERCENT,
                )
                self._push(time_ms, "ORIGIN", (node.id, message_spec, config.hop_limit, False))
                time_ms += max(1_000.0, self.rng.expovariate(1.0 / message_mean_ms))

    def _build_link_cache(self, cancelled: Callable[[], bool]) -> bool:
        nodes = list(self.nodes.values())
        for source in nodes:
            if cancelled():
                return False
            neighbors: list[tuple[Node, LinkResult]] = []
            for target_index, target in enumerate(nodes):
                if target_index % 32 == 0 and cancelled():
                    return False
                if target.id == source.id:
                    continue
                compatible, _reason = self.model.radios_compatible(source, target)
                if not compatible:
                    continue
                horizontal = math.hypot(target.x - source.x, target.y - source.y)
                if horizontal > self.model.unobstructed_range_m(source, target) * 1.2:
                    continue
                link = self.model.link(source, target)
                # A blocked terrain/obstacle path can still have a mathematically
                # high pre-block margin. Never retain it as a live RF neighbor.
                if link.compatible and link.margin_db >= MIN_DECODE_MARGIN_DB:
                    neighbors.append((target, link))
            self._links_by_source[source.id] = neighbors
            self._links_by_target[source.id] = {
                target.id: (target, link) for target, link in neighbors
            }
        return True

    def _cached_link(self, source_id: str, target_id: str) -> LinkResult | None:
        cached = self._links_by_target.get(source_id, {}).get(target_id)
        return cached[1] if cached is not None else None

    def _valid_directed_route(self, packet: PacketConfig, route: list[str]) -> bool:
        if (
            len(route) < 2
            or route[0] != packet.source_id
            or route[-1] != packet.destination_id
            or len(set(route)) != len(route)
        ):
            return False
        return all(
            first in self.nodes and second in self.nodes and self._cached_link(first, second) is not None
            for first, second in zip(route, route[1:])
        )

    @staticmethod
    def _path_from_arrivals(arrivals: dict[str, dict[str, Any]], destination_id: str) -> list[str]:
        route: list[str] = []
        visited: set[str] = set()
        current = destination_id
        while current and current not in visited:
            visited.add(current)
            route.append(current)
            current = str(arrivals.get(current, {}).get("via", ""))
        return list(reversed(route))

    def _test_path(self, test: LiveMeshTestResult) -> list[str]:
        return self._path_from_arrivals(test.reached, test.packet.destination_id)

    @staticmethod
    def _response_for_port(port: str) -> tuple[str, int] | None:
        """Ports with a firmware module response when decoded.want_response is set."""
        sizes = {
            "NODEINFO_APP": 48,
            "POSITION_APP": 32,
            "TELEMETRY_APP": 40,
            "NEIGHBORINFO_APP": 56,
            "TRACEROUTE_APP": 48,
            "ADMIN_APP": 64,
        }
        payload_bytes = sizes.get(port)
        return (port, payload_bytes) if payload_bytes is not None else None

    @staticmethod
    def _response_hop_limit(hops_used: int, request_hop_limit: int) -> int:
        # RoutingModule::getHopLimitForResponse: use the path already consumed
        # plus margin, bounded by the configured local hop limit.
        if hops_used <= 0:
            return 0
        return max(0, min(7, request_hop_limit))

    def _test_event_kind(self, state: _PacketState, base: str) -> str:
        return f"{state.response_kind} {base}" if state.response_kind else base

    def _queue_return_packet(
        self,
        time_ms: float,
        request: _PacketState,
        responder_id: str,
        hop: int,
    ) -> None:
        """Put firmware ACK/NAK/module replies onto the same RF event queue."""
        if request.test_id is None:
            return
        test = self._tests.get(request.test_id)
        if test is None:
            return
        response = self._response_for_port(request.packet.port) if request.packet.want_response else None
        if response is not None:
            response_kind, payload_bytes = "RESPONSE", response[1]
            port = response[0]
        elif request.packet.want_response:
            response_kind, payload_bytes, port = "NAK", 8, "ROUTING_APP"
        elif request.packet.want_ack:
            response_kind, payload_bytes, port = "ACK", 8, "ROUTING_APP"
        else:
            return

        self._next_packet_id += 1
        reply_packet = PacketConfig(
            source_id=responder_id,
            destination_id=request.origin_id,
            payload=response_kind.title(),
            payload_bytes=payload_bytes,
            hop_limit=self._response_hop_limit(hop, request.packet.hop_limit),
            port=port,
            # Firmware makes text-message ACKs reliable too.  We model its TX
            # priority and airtime, but do not synthesize an extra zero-hop ACK.
            want_ack=(response_kind == "ACK" and request.packet.port in {"TEXT_MESSAGE_APP", "TEXT_MESSAGE_COMPRESSED_APP"}),
            want_response=False,
            channel=request.packet.channel,
        )
        spec = _TrafficSpec(response_kind, port, payload_bytes, 0.0, MAX_CHANNEL_LIMIT_PERCENT)
        state = _PacketState(
            self._next_packet_id,
            spec,
            reply_packet,
            responder_id,
            {responder_id},
            test_id=request.test_id,
            arrivals={responder_id: {"time_ms": time_ms, "hop": 0, "via": ""}},
            response_kind=response_kind,
            reply_to_packet_id=request.packet_id,
        )
        self._packet_states.append(state)
        test.status = "AWAITING RESPONSE" if response_kind == "RESPONSE" else "AWAITING ACK"
        self._test_event(
            state,
            time_ms,
            f"{response_kind} QUEUED",
            responder_id,
            request.origin_id,
            hop,
            detail="return packet queued on the same RF channel",
        )
        # ACK/NAK/response packets are high-priority firmware queue entries.
        self._push(time_ms + 1.0, "TX", _TxTask(state, responder_id, "", reply_packet.hop_limit, 0))

    def _complete_return_packet(self, time_ms: float, state: _PacketState) -> None:
        if state.test_id is None:
            return
        test = self._tests.get(state.test_id)
        request = self._test_request_states.get(state.test_id)
        if test is None or request is None:
            return
        if state.response_kind == "NAK":
            test.status = "NAK RECEIVED"
            test.complete = True
            return
        if state.response_kind == "RESPONSE":
            test.response_received = True
        test.acknowledged = True
        if test.routing_mode in {"DM_DISCOVERY_FLOOD", "DM_FALLBACK_FLOOD"}:
            reverse = self._path_from_arrivals(state.arrivals, request.origin_id)
            route = list(reversed(reverse))
            if self._valid_directed_route(test.packet, route):
                test.learned_route = route
                self._learned_routes[test.route_key] = list(route)
                self._test_event(
                    state,
                    time_ms,
                    "ROUTE LEARNED",
                    request.origin_id,
                    state.origin_id,
                    len(route) - 1,
                    detail="a return packet reached the sender; future DMs use the confirmed next-hop path",
                )
        test.status = "RESPONSE RECEIVED" if state.response_kind == "RESPONSE" else "ACKNOWLEDGED"
        test.complete = True

    def _fallback_directed_test(self, time_ms: float, state: _PacketState, detail: str) -> None:
        """Discard a failed learned path and retry this DM as managed discovery."""
        if not state.directed_route or state.test_id is None:
            return
        test = self._tests.get(state.test_id)
        if test is None:
            return
        route_key = test.route_key
        if route_key:
            self._learned_routes.pop(route_key, None)
        test.routing_mode = "DM_FALLBACK_FLOOD"
        test.invalidated_route_key = route_key
        test.learned_route = []
        state.directed_route = []
        state.directed_index = 0
        state.seen = {state.origin_id}
        self._test_event(state, time_ms, "ROUTE FALLBACK", state.origin_id, detail=detail)
        self._push(
            time_ms + 1.0,
            "TX",
            _TxTask(state, state.origin_id, "", state.packet.hop_limit, 0),
        )

    def _test_event(
        self, state: _PacketState, time_ms: float, kind: str, node_id: str, peer_id: str = "", hop: int = 0,
        link: LinkResult | None = None, detail: str = ""
    ) -> None:
        if state.test_id is None:
            return
        test = self._tests.get(state.test_id)
        if test is None or len(test.events) >= 2500:
            return
        test.events.append(LiveMeshTestEvent(
            time_ms, kind, node_id, peer_id, hop,
            link.rssi_dbm if link else 0.0, link.snr_db if link else 0.0,
            link.margin_db if link else 0.0, detail,
        ))

    def _origin(self, time_ms: float, node_id: str, spec: _TrafficSpec, hop_limit: int, recurring: bool = False) -> None:
        assert self._result is not None
        node = self.nodes.get(node_id)
        if node is None:
            return
        self._result.offered_packets += 1
        utilization = self._channel.utilization(node_id, time_ms)
        self._record_peak(time_ms, utilization)
        if utilization >= spec.channel_limit_percent:
            self._result.throttled += 1
            frame = self._frame(time_ms)
            frame.throttle_count += 1
            frame.traffic_throttles[spec.kind] = frame.traffic_throttles.get(spec.kind, 0) + 1
            if len(frame.throttled) < MAX_FRAME_THROTTLES:
                frame.throttled.append(node_id)
            if recurring:
                self._push(time_ms + max(1_000.0, spec.interval_ms), "ORIGIN", (node_id, spec, hop_limit, True))
            return

        self._next_packet_id += 1
        packet = PacketConfig(
            source_id=node_id,
            destination_id="BROADCAST",
            payload=spec.kind.title(),
            payload_bytes=spec.payload_bytes,
            hop_limit=max(0, min(7, int(hop_limit))),
            port=spec.port,
            want_ack=False,
            channel=node.channel,
        )
        state = _PacketState(self._next_packet_id, spec, packet, node_id, {node_id})
        self._packet_states.append(state)
        self._result.originated_packets += 1
        self._result.traffic_counts[spec.kind] = self._result.traffic_counts.get(spec.kind, 0) + 1

        symbol_ms = (2 ** max(5, min(12, node.radio.spreading_factor))) / max(1.0, node.radio.bandwidth_khz)
        slot_ms = max(1.0, 2.5 * symbol_ms + 7.6)
        contention_window = max(3, min(8, round(3 + utilization / 100.0 * 5)))
        delay = self.rng.randrange(max(1, 2**contention_window)) * slot_ms
        self._push(time_ms + delay, "TX", _TxTask(state, node_id, "", packet.hop_limit, 0))
        if recurring:
            interval = spec.interval_ms
            if spec.kind == "MESSAGE":
                interval = max(1_000.0, self.rng.expovariate(1.0 / max(1_000.0, interval)))
            self._push(time_ms + interval, "ORIGIN", (node_id, spec, hop_limit, True))

    def _relay_delay_ms(self, node: Node, snr: float) -> float:
        symbol_ms = (2 ** max(5, min(12, node.radio.spreading_factor))) / max(1.0, node.radio.bandwidth_khz)
        slot_ms = max(1.0, 2.5 * symbol_ms + 7.6)
        cw = round(3 + (max(-20.0, min(10.0, snr)) + 20.0) / 30.0 * 5)
        if node.role in {"ROUTER", "ROUTER_CLIENT", "REPEATER"}:
            return self.rng.randrange(max(1, 2 * cw)) * slot_ms
        delay = 16 * slot_ms + self.rng.randrange(max(1, 2**cw)) * slot_ms
        if node.role in {"ROUTER_LATE", "CLIENT_BASE"}:
            delay += (2**cw) * slot_ms
        return delay

    def _transmit(self, time_ms: float, task: _TxTask) -> None:
        assert self._result is not None
        if task.token is not None and task.token.cancelled:
            self._result.cancelled_relays += 1
            self._test_event(task.packet, time_ms, "CANCELLED", task.node_id, task.via_id, task.hop, detail="relay suppressed after duplicate was heard")
            return
        transmitter = self.nodes.get(task.node_id)
        if transmitter is None or self._result.transmissions >= MAX_LIVE_TRANSMISSIONS:
            self._result.truncated = self._result.transmissions >= MAX_LIVE_TRANSMISSIONS
            return

        airtime = self.model.airtime_ms(transmitter, task.packet.packet.payload_bytes)
        end_ms = time_ms + airtime
        self._result.transmissions += 1
        if task.packet.test_id is not None:
            test = self._tests[task.packet.test_id]
            test.transmissions += 1
            detail = "transmitted into current live channel load"
            if task.packet.response_kind:
                detail = "high-priority return packet transmitted into current live channel load"
            self._test_event(
                task.packet,
                time_ms,
                self._test_event_kind(task.packet, "TX"),
                transmitter.id,
                task.via_id,
                task.hop,
                detail=detail,
            )
        utilization = self._channel.add(transmitter.id, time_ms, airtime)
        self._record_peak(time_ms, utilization)
        frame = self._frame(time_ms)
        frame.transmission_count += 1
        kind = task.packet.spec.kind
        frame.traffic_transmissions[kind] = frame.traffic_transmissions.get(kind, 0) + 1
        if len(frame.transmitters) < MAX_FRAME_TRANSMITTERS:
            frame.transmitters.append((transmitter.id, kind))

        receivers = self._links_by_source.get(transmitter.id, ())
        if task.packet.directed_route:
            next_index = task.packet.directed_index + 1
            if next_index >= len(task.packet.directed_route):
                return
            next_hop_id = task.packet.directed_route[next_index]
            next_hop = self._links_by_target.get(transmitter.id, {}).get(next_hop_id)
            if next_hop is None:
                self._fallback_directed_test(
                    time_ms,
                    task.packet,
                    "stored next hop is no longer RF-compatible; retrying with managed discovery",
                )
                return
            receivers = (next_hop,)

        stochastic = self.scenario.environment.stochastic
        shadowing_sigma_db = self.scenario.environment.shadowing_sigma_db
        capture_threshold_db = self.scenario.environment.capture_threshold_db
        for receiver, link in receivers:
            heard_utilization = self._channel.add(receiver.id, time_ms, airtime)
            self._result.peak_channel_utilization = max(
                self._result.peak_channel_utilization, heard_utilization
            )
            frame.peak_channel_utilization = max(
                frame.peak_channel_utilization, heard_utilization
            )
            shadow = (
                self.rng.gauss(0.0, shadowing_sigma_db)
                if stochastic
                else 0.0
            )
            event_margin = link.margin_db + shadow
            success = link.compatible and event_margin >= MIN_DECODE_MARGIN_DB
            if stochastic:
                event_probability = 1.0 / (
                    1.0 + math.exp(-max(-40.0, min(40.0, event_margin)) / 2.0)
                )
                success = success and self.rng.random() <= event_probability
            if not success:
                self._result.dropped += 1
                frame.drop_count += 1
                frame.traffic_drops[kind] = frame.traffic_drops.get(kind, 0) + 1
                if task.packet.test_id is not None:
                    test = self._tests[task.packet.test_id]
                    test.dropped += 1
                    why = link.reason if not link.compatible else f"RF margin {event_margin:.1f} dB; fade/loss prevented decode"
                    self._test_event(
                        task.packet,
                        time_ms,
                        self._test_event_kind(task.packet, "RF DROP"),
                        receiver.id,
                        transmitter.id,
                        task.hop + 1,
                        link,
                        why,
                    )
                    if task.packet.directed_route:
                        self._fallback_directed_test(
                            time_ms,
                            task.packet,
                            "stored next hop did not decode; retrying with managed discovery",
                        )
                        return
                continue

            reception = _Reception(
                packet=task.packet,
                transmitter_id=transmitter.id,
                receiver_id=receiver.id,
                start_ms=time_ms,
                end_ms=end_ms,
                hops_left=task.hops_left,
                hop=task.hop + 1,
                link=link,
                rssi_dbm=link.rssi_dbm + shadow,
                snr_db=link.snr_db + shadow,
            )
            active = self._active_receptions[receiver.id]
            active[:] = [old for old in active if old.end_ms > time_ms]
            for old in active:
                difference = old.rssi_dbm - reception.rssi_dbm
                if abs(difference) < capture_threshold_db:
                    old.collided = True
                    reception.collided = True
                elif difference >= capture_threshold_db:
                    reception.collided = True
                else:
                    old.collided = True
            active.append(reception)
            self._push(end_ms, "RX_END", reception)

        if task.packet.test_id is not None and not task.packet.directed_route:
            # Explain viable-but-not-cached links too. This is intentionally only
            # done for a user test, never for routine traffic.
            cached = self._links_by_target.get(transmitter.id, {})
            for receiver in self.nodes.values():
                if receiver.id == transmitter.id or receiver.id in cached:
                    continue
                link = self.model.link(transmitter, receiver)
                reason = (
                    link.reason
                    if not link.compatible
                    else (
                        f"RF margin {link.margin_db:.1f} dB below calibrated "
                        f"{MIN_DECODE_MARGIN_DB:g} dB field threshold"
                    )
                )
                self._test_event(task.packet, time_ms, "UNREACHABLE", receiver.id, transmitter.id, task.hop + 1, link, reason)

    def _receive(self, time_ms: float, reception: _Reception) -> None:
        assert self._result is not None
        active = self._active_receptions.get(reception.receiver_id, [])
        if reception in active:
            active.remove(reception)
        if reception.collided:
            self._result.collisions += 1
            frame = self._frame(time_ms)
            frame.collision_count += 1
            kind = reception.packet.spec.kind
            frame.traffic_collisions[kind] = frame.traffic_collisions.get(kind, 0) + 1
            if len(frame.collisions) < MAX_FRAME_COLLISIONS:
                frame.collisions.append(reception.receiver_id)
            if reception.packet.test_id is not None:
                test = self._tests[reception.packet.test_id]
                test.collisions += 1
                self._test_event(
                    reception.packet,
                    time_ms,
                    self._test_event_kind(reception.packet, "COLLISION"),
                    reception.receiver_id,
                    reception.transmitter_id,
                    reception.hop,
                    reception.link,
                    "overlapping live transmission exceeded capture tolerance",
                )
                if reception.packet.directed_route:
                    self._fallback_directed_test(
                        time_ms,
                        reception.packet,
                        "stored next hop collided; retrying with managed discovery",
                    )
            return

        state = reception.packet
        receiver = self.nodes.get(reception.receiver_id)
        if receiver is None:
            return
        if receiver.id in state.seen:
            self._result.duplicates += 1
            token = state.pending_tokens.get(receiver.id)
            if token is not None and self._can_cancel(receiver):
                token.cancelled = True
            self._test_event(state, time_ms, "DUPLICATE", receiver.id, reception.transmitter_id, reception.hop, reception.link, "already heard; pending relay cancelled when allowed")
            return

        state.seen.add(receiver.id)
        self._result.receptions += 1
        state.arrivals[receiver.id] = {
            "time_ms": time_ms,
            "hop": reception.hop,
            "via": reception.transmitter_id,
        }
        if state.test_id is not None:
            test = self._tests[state.test_id]
            test.receptions += 1
            if not state.response_kind:
                test.reached[receiver.id] = dict(state.arrivals[receiver.id])
            self._test_event(
                state,
                time_ms,
                self._test_event_kind(state, "RX"),
                receiver.id,
                reception.transmitter_id,
                reception.hop,
                reception.link,
                "received successfully",
            )
            if receiver.id == state.packet.destination_id:
                if state.response_kind:
                    self._complete_return_packet(time_ms, state)
                elif state.packet.destination_id != "BROADCAST":
                    self._queue_return_packet(time_ms, state, receiver.id, reception.hop)
        frame = self._frame(time_ms)
        frame.reception_count += 1
        if len(frame.receptions) < MAX_FRAME_RECEPTIONS:
            frame.receptions.append(
                (reception.transmitter_id, receiver.id, state.spec.kind, reception.hop)
            )

        if state.directed_route:
            state.directed_index += 1
            if receiver.id == state.packet.destination_id:
                return
            self._push(
                time_ms + self._relay_delay_ms(receiver, reception.snr_db),
                "TX",
                _TxTask(state, receiver.id, reception.transmitter_id, reception.hops_left - 1, reception.hop),
            )
            return

        if (
            (state.packet.destination_id != "BROADCAST" and receiver.id == state.packet.destination_id)
            or reception.hop >= state.packet.hop_limit
        ):
            if reception.hop >= state.packet.hop_limit:
                self._test_event(
                    state, time_ms, "HOP LIMIT", receiver.id, reception.transmitter_id, reception.hop,
                    reception.link, f"received at H{reception.hop}; configured hop limit is H{state.packet.hop_limit}",
                )
            return
        decoded = receiver.channel == state.packet.channel
        if not self._can_relay(receiver, state.packet, decoded):
            self._test_event(state, time_ms, "NO RELAY", receiver.id, reception.transmitter_id, reception.hop, reception.link, "role, rebroadcast mode, or opaque channel prevents relay")
            return
        token = _RelayToken()
        state.pending_tokens[receiver.id] = token
        delay = self._relay_delay_ms(receiver, reception.snr_db)
        self._push(
            time_ms + delay,
            "TX",
            _TxTask(
                state,
                receiver.id,
                reception.transmitter_id,
                reception.hops_left - 1,
                reception.hop,
                token,
            ),
        )

    def _process_until(self, target_time_ms: float, cancelled: Callable[[], bool] | None = None) -> None:
        """Advance the shared RF timeline.  Used by both finite and live modes."""
        cancelled = cancelled or (lambda: False)
        assert self._result is not None
        while self._events and self._events[0][0] <= target_time_ms:
            if cancelled() or self._result.truncated:
                break
            time_ms, _sequence, kind, payload = heapq.heappop(self._events)
            state = self._event_packet_state(kind, payload)
            if state is not None:
                state.pending_events = max(0, state.pending_events - 1)
            if kind == "ORIGIN":
                node_id, spec, hop_limit, recurring = payload
                self._origin(time_ms, node_id, spec, hop_limit, recurring)
            elif kind == "TEST_ORIGIN":
                self._start_injected_test(time_ms, payload)
            elif kind == "TX":
                self._transmit(time_ms, payload)
            elif kind == "RX_END":
                self._receive(time_ms, payload)
        self._runtime_time_ms = max(self._runtime_time_ms, target_time_ms)
        self._prune_finished_runtime_packets(target_time_ms)
        self._finish_tests(target_time_ms)

    def prepare_runtime(self, config: LiveMeshConfig | None = None, cancelled: Callable[[], bool] | None = None) -> bool:
        """Prepare an endless, event-driven live mesh session."""
        config = config or self.scenario.live_mesh
        cancelled = cancelled or (lambda: False)
        self._runtime = True
        self._runtime_config = config
        self._runtime_frames.clear()
        self._runtime_time_ms = 0.0
        self._last_state_prune_ms = 0.0
        self._result = LiveMeshResult(duration_ms=0.0, frames=[])
        if not self.nodes:
            return True
        if not self._build_link_cache(cancelled):
            return False
        for node in self.nodes.values():
            for spec in self._specs_for(node, config):
                # A live mesh joins an already-running network.  Spread every
                # node across its full reporting interval rather than producing
                # a synchronized startup flood in the first minute.
                phase = self.rng.uniform(0.0, max(1_000.0, spec.interval_ms))
                self._push(phase, "ORIGIN", (node.id, spec, config.hop_limit, True))
            message_spec = _TrafficSpec(
                "MESSAGE", "TEXT_MESSAGE_APP", 32,
                max(1.0, config.message_interval_minutes) * 60_000.0 * self._traffic_scale(config.traffic_profile),
                MAX_CHANNEL_LIMIT_PERCENT,
            )
            phase = self.rng.uniform(0.0, max(1_000.0, message_spec.interval_ms))
            self._push(phase, "ORIGIN", (node.id, message_spec, config.hop_limit, True))

        # A real mesh is already active when observation begins.  Seed only a
        # couple of staggered beacons so the UI proves the live session is
        # working promptly, while every node still keeps its independent long
        # reporting phase and does not create a synchronized startup flood.
        startup_nodes = list(self.nodes.values())
        self.rng.shuffle(startup_nodes)
        for index, node in enumerate(startup_nodes[: min(2, len(startup_nodes))]):
            startup_spec = _TrafficSpec(
                "NODEINFO", "NODEINFO_APP", 48,
                max(1.0, config.nodeinfo_interval_minutes) * 60_000.0,
                MAX_CHANNEL_LIMIT_PERCENT,
            )
            startup_time = 1_500.0 + index * 3_500.0 + self.rng.uniform(0.0, 1_500.0)
            self._push(startup_time, "ORIGIN", (node.id, startup_spec, config.hop_limit, False))
        return True

    def advance_runtime(self, simulated_ms: float, cancelled: Callable[[], bool] | None = None) -> list[LiveMeshFrame]:
        if not self._runtime:
            raise RuntimeError("Live runtime has not been prepared")
        previous = int(self._runtime_time_ms / self._runtime_frame_ms)
        self._process_until(simulated_ms, cancelled)
        current = int(self._runtime_time_ms / self._runtime_frame_ms)
        frames = [self._runtime_frames.pop(index) for index in range(previous, current + 1) if index in self._runtime_frames]
        return frames

    def _start_injected_test(self, time_ms: float, state: _PacketState) -> None:
        """Hold a user send until the source may legally start transmitting."""
        test = self._tests.get(state.test_id or -1)
        source = self.nodes.get(state.origin_id)
        if test is None or source is None or test.complete:
            return
        utilization = self._channel.utilization(source.id, time_ms)
        if utilization >= MAX_CHANNEL_LIMIT_PERCENT:
            test.status = "WAITING FOR CHANNEL"
            test.throttled += 1
            frame = self._frame(time_ms)
            frame.throttle_count += 1
            frame.traffic_throttles["TEST"] = frame.traffic_throttles.get("TEST", 0) + 1
            if not test.events or test.events[-1].kind != "CHANNEL WAIT":
                self._test_event(
                    state, time_ms, "CHANNEL WAIT", source.id,
                    detail=f"local channel utilization {utilization:.1f}% exceeds 40%; retrying in 1 second",
                )
            self._push(time_ms + 1_000.0, "TEST_ORIGIN", state)
            return
        test.status = "IN FLIGHT"
        test.time_ms = time_ms
        test.reached[source.id] = {"time_ms": time_ms, "hop": 0, "via": ""}
        self._test_event(state, time_ms, "QUEUED", source.id, detail=f"transmitting at {utilization:.1f}% local channel utilization")
        self._push(time_ms + 1.0, "TX", _TxTask(state, source.id, "", state.packet.hop_limit, 0))

    def inject_packet(self, packet: PacketConfig) -> LiveMeshTestResult:
        """Inject a user packet into the current channel timeline, preserving collisions/utilization."""
        if not self._runtime or self._result is None:
            raise RuntimeError("Live runtime has not been prepared")
        source = self.nodes.get(packet.source_id)
        self._next_test_id += 1
        test = LiveMeshTestResult(self._next_test_id, packet, self._runtime_time_ms)
        if packet.destination_id != "BROADCAST":
            test.route_key = dm_route_key(packet.source_id, packet.destination_id)
            stored_route = self._learned_routes.get(test.route_key, [])
            if stored_route and self._valid_directed_route(packet, stored_route):
                test.routing_mode = "DM_LEARNED"
                test.learned_route = list(stored_route)
            elif stored_route:
                self._learned_routes.pop(test.route_key, None)
                test.routing_mode = "DM_FALLBACK_FLOOD"
                test.invalidated_route_key = test.route_key
            else:
                test.routing_mode = "DM_DISCOVERY_FLOOD"
        self._tests[test.test_id] = test
        if source is None:
            test.status = "REJECTED"
            test.complete = True
            test.events.append(LiveMeshTestEvent(self._runtime_time_ms, "REJECTED", packet.source_id, detail="source is offline or absent"))
            return test
        spec = _TrafficSpec("TEST", packet.port, packet.payload_bytes, 0.0, MAX_CHANNEL_LIMIT_PERCENT)
        self._next_packet_id += 1
        state = _PacketState(
            self._next_packet_id,
            spec,
            packet,
            source.id,
            {source.id},
            test_id=test.test_id,
            directed_route=list(test.learned_route),
            arrivals={source.id: {"time_ms": self._runtime_time_ms, "hop": 0, "via": ""}},
        )
        self._packet_states.append(state)
        self._test_request_states[test.test_id] = state
        test.status = "WAITING FOR CHANNEL"
        self._test_event(state, self._runtime_time_ms, "QUEUED", source.id, detail="queued behind live channel activity")
        self._push(self._runtime_time_ms + 1.0, "TEST_ORIGIN", state)
        return test

    def _finish_tests(self, time_ms: float) -> None:
        for test in self._tests.values():
            if test.complete or test.status == "WAITING FOR CHANNEL" or time_ms - test.time_ms < 15_000.0:
                continue
            if test.packet.destination_id == "BROADCAST":
                test.status = f"COMPLETE · {max(0, len(test.reached) - 1)} nodes received"
            elif test.packet.destination_id in test.reached:
                if test.packet.want_response:
                    test.status = "DELIVERED · RESPONSE NOT RECEIVED"
                    test.events.append(LiveMeshTestEvent(
                        time_ms, "RESPONSE FAILED", test.packet.source_id, test.packet.destination_id,
                        detail="the destination decoded the request but no module reply returned through the live RF channel",
                    ))
                elif test.packet.want_ack:
                    test.status = "DELIVERED · ACK NOT RECEIVED"
                    test.events.append(LiveMeshTestEvent(
                        time_ms, "ACK FAILED", test.packet.source_id, test.packet.destination_id,
                        detail="the destination decoded the packet but its ROUTING_APP ACK did not return through the live RF channel",
                    ))
                else:
                    test.status = "DELIVERED"
            else:
                test.status = "NOT DELIVERED"
                test.events.append(LiveMeshTestEvent(time_ms, "RESULT", test.packet.destination_id, detail="destination never decoded the packet in the available hop budget"))
            test.complete = True

    def runtime_snapshot(self) -> dict[str, Any]:
        result = self._result or LiveMeshResult(0.0, [])
        return {
            "time_ms": self._runtime_time_ms,
            "transmissions": result.transmissions,
            "receptions": result.receptions,
            "collisions": result.collisions,
            "dropped": result.dropped,
            "throttled": result.throttled,
            "peak": result.peak_channel_utilization,
            "tests": list(self._tests.values()),
            "learned_routes": {key: list(route) for key, route in self._learned_routes.items()},
            "node_utilization": {node_id: self._channel.utilization(node_id, self._runtime_time_ms) for node_id in self.nodes},
        }

    def run(
        self,
        config: LiveMeshConfig | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> LiveMeshResult:
        config = config or self.scenario.live_mesh
        cancelled = cancelled or (lambda: False)
        source_duration_ms = max(1, min(24 * 60, int(config.duration_minutes))) * 60_000.0
        timeline_ms = source_duration_ms + 120_000.0
        frames = [
            LiveMeshFrame(time_ms=timeline_ms * index / LIVE_FRAME_COUNT)
            for index in range(LIVE_FRAME_COUNT)
        ]
        self._result = LiveMeshResult(duration_ms=timeline_ms, frames=frames)
        if not self.nodes:
            return self._result

        if not self._build_link_cache(cancelled):
            return self._result
        self._schedule_origins(config, source_duration_ms)

        self._process_until(timeline_ms, cancelled)

        self._result.packets_heard = sum(len(state.seen) > 1 for state in self._packet_states)
        return self._result
