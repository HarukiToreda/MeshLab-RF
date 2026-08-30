from __future__ import annotations

import binascii
import csv
import struct
from collections import defaultdict
from pathlib import Path
from typing import Iterable

SURVEY_SCHEMA = "2"
SURVEY_RECORD_MAGIC = 0x3152464D
SURVEY_RECORD_SIZE = 80
SURVEY_RECORD_STRUCT = struct.Struct("<IBBBBIIIIIIiiiHBBiiiHhhhhIHI")
SURVEY_RECORD_V1_SIZE = 128
SURVEY_RECORD_V1_STRUCT = struct.Struct("<IBBBBIIIIQQiiiHBBiiiHBBhhhhIIHBBb31sI")
DEFAULT_RADIO_PROFILE = {
    "frequency_hz": 906_875_000,
    "bandwidth_khz": 250,
    "spreading_factor": 11,
    "coding_rate": 5,
    "tx_power_dbm": 22,
}
SURVEY_ROLES = {1: "mobile", 2: "base"}
SURVEY_EVENTS = {
    0: "BOOT",
    1: "SEND",
    2: "PROBE_RX",
    3: "REPLY_TX",
    4: "REPLY_RX",
    5: "TIMEOUT",
    6: "STORAGE_FULL",
}
SURVEY_REQUIRED_FIELDS = {
    "schema",
    "role",
    "event",
    "session_id",
    "sequence",
    "epoch_s",
    "node_num",
    "peer_num",
    "local_gps_lock",
    "local_latitude_i",
    "local_longitude_i",
    "remote_gps_lock",
    "remote_latitude_i",
    "remote_longitude_i",
    "local_hdop_centi",
    "remote_hdop_centi",
    "local_rx_valid",
    "local_rx_rssi_dbm",
    "local_rx_snr_centi_db",
    "remote_rx_valid",
    "remote_rx_rssi_dbm",
    "remote_rx_snr_centi_db",
}


class SurveyLogError(ValueError):
    pass


def decode_survey_records(
    data: bytes,
    record_size: int | None = None,
    radio_profile: dict[str, int] | None = None,
) -> tuple[list[dict[str, str]], int]:
    if record_size is None:
        if not data:
            return [], 0
        record_size = SURVEY_RECORD_SIZE if data[4] == 2 else SURVEY_RECORD_V1_SIZE
    if record_size not in (SURVEY_RECORD_SIZE, SURVEY_RECORD_V1_SIZE):
        raise SurveyLogError(f"unsupported survey record size {record_size}")
    if len(data) % record_size:
        raise SurveyLogError(
            f"survey dump is {len(data)} bytes; expected a multiple of {record_size}"
        )

    profile = {**DEFAULT_RADIO_PROFILE, **(radio_profile or {})}
    rows: list[dict[str, str]] = []
    invalid = 0
    for offset in range(0, len(data), record_size):
        raw = data[offset : offset + record_size]
        values = (SURVEY_RECORD_STRUCT if record_size == SURVEY_RECORD_SIZE else SURVEY_RECORD_V1_STRUCT).unpack(raw)
        expected_version = 2 if record_size == SURVEY_RECORD_SIZE else 1
        if values[0] != SURVEY_RECORD_MAGIC or values[1] != expected_version:
            invalid += 1
            continue
        if (binascii.crc32(raw[:-4]) & 0xFFFFFFFF) != values[-1]:
            invalid += 1
            continue

        role, event, flags = values[2:5]
        session_id, sequence, epoch_s, uptime_ms, node_num, peer_num = values[5:11]
        local_latitude_i, local_longitude_i, local_altitude_cm, local_hdop_centi = values[11:15]
        local_satellites = values[15]
        if expected_version == 2:
            remote_satellites = values[16]
            remote_latitude_i, remote_longitude_i, remote_altitude_cm, remote_hdop_centi = values[17:21]
            local_rssi_dbm, local_snr_centi_db, remote_rssi_dbm, remote_snr_centi_db = values[21:25]
            packet_id = values[25]
            frequency_hz = profile["frequency_hz"]
            bandwidth_khz = profile["bandwidth_khz"]
            spreading_factor = profile["spreading_factor"]
            coding_rate = profile["coding_rate"]
            tx_power_dbm = profile["tx_power_dbm"]
        else:
            remote_latitude_i, remote_longitude_i, remote_altitude_cm, remote_hdop_centi = values[17:21]
            remote_satellites = values[21]
            local_rssi_dbm, local_snr_centi_db, remote_rssi_dbm, remote_snr_centi_db = values[23:27]
            packet_id, frequency_hz, bandwidth_khz, spreading_factor, coding_rate, tx_power_dbm = values[27:33]
        if role not in SURVEY_ROLES or event not in SURVEY_EVENTS:
            invalid += 1
            continue
        rows.append(
            {
                "schema": str(expected_version),
                "role": SURVEY_ROLES[role],
                "event": SURVEY_EVENTS[event],
                "session_id": str(session_id),
                "sequence": str(sequence),
                "epoch_s": str(epoch_s),
                "uptime_ms": str(uptime_ms),
                "node_num": str(node_num),
                "peer_num": str(peer_num),
                "local_gps_lock": str(int(bool(flags & 0x01))),
                "local_latitude_i": str(local_latitude_i),
                "local_longitude_i": str(local_longitude_i),
                "local_altitude_m": str(round(local_altitude_cm / 100)),
                "local_hdop_centi": str(local_hdop_centi),
                "local_satellites": str(local_satellites),
                "remote_gps_lock": str(int(bool(flags & 0x02))),
                "remote_latitude_i": str(remote_latitude_i),
                "remote_longitude_i": str(remote_longitude_i),
                "remote_altitude_m": str(round(remote_altitude_cm / 100)),
                "remote_hdop_centi": str(remote_hdop_centi),
                "remote_satellites": str(remote_satellites),
                "local_rx_valid": str(int(bool(flags & 0x04))),
                "local_rx_rssi_dbm": str(local_rssi_dbm),
                "local_rx_snr_centi_db": str(local_snr_centi_db),
                "remote_rx_valid": str(int(bool(flags & 0x08))),
                "remote_rx_rssi_dbm": str(remote_rssi_dbm),
                "remote_rx_snr_centi_db": str(remote_snr_centi_db),
                "reply_sent": str(int(bool(flags & 0x10))),
                "packet_id": str(packet_id),
                "channel_utilization_centi_pct": "0",
                "tx_utilization_centi_pct": "0",
                "region": "1",
                "modem_preset": "0",
                "frequency_hz": str(frequency_hz),
                "tx_power_dbm": str(tx_power_dbm),
                "bandwidth_khz": str(bandwidth_khz),
                "spreading_factor": str(spreading_factor),
                "coding_rate": str(coding_rate),
            }
        )
    return rows, invalid


def _integer(row: dict[str, str] | None, field: str, default: int = 0) -> int:
    if not row:
        return default
    try:
        return int(row.get(field, "") or default)
    except ValueError:
        return default


def _gps_position(
    row: dict[str, str] | None,
    prefix: str,
) -> tuple[bool, float | None, float | None]:
    locked = bool(_integer(row, f"{prefix}_gps_lock"))
    if not locked:
        return False, None, None
    return (
        True,
        _integer(row, f"{prefix}_latitude_i") * 1e-7,
        _integer(row, f"{prefix}_longitude_i") * 1e-7,
    )


def _rx_metrics(
    row: dict[str, str] | None,
    prefix: str,
) -> tuple[float | None, float | None]:
    if not row or not _integer(row, f"{prefix}_rx_valid"):
        return None, None
    return (
        float(_integer(row, f"{prefix}_rx_rssi_dbm")),
        _integer(row, f"{prefix}_rx_snr_centi_db") / 100.0,
    )


def merge_survey_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    known_base_nodes: set[int] = set()
    grouped: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("role") == "base":
            node_num = _integer(row, "node_num")
            if node_num:
                known_base_nodes.add(node_num)
        sequence = _integer(row, "sequence")
        if sequence:
            grouped[(_integer(row, "session_id"), sequence)].append(row)

    measurements: list[dict[str, object]] = []
    for (session_id, sequence), group in sorted(grouped.items()):
        send: dict[str, str] | None = None
        completion: dict[str, str] | None = None
        base_receives: list[dict[str, str]] = []
        base_receives_by_node: dict[int, dict[str, str]] = {}
        base_transmits_by_node: dict[int, dict[str, str]] = {}
        mobile_replies_by_peer: dict[int, dict[str, str]] = {}
        for row in group:
            role = row.get("role")
            event = row.get("event")
            if role == "mobile":
                if event == "SEND":
                    send = row
                elif event in {"REPLY_RX", "TIMEOUT"}:
                    completion = row
                if event == "REPLY_RX":
                    mobile_replies_by_peer[_integer(row, "peer_num")] = row
            elif role == "base":
                if event == "PROBE_RX":
                    base_receives.append(row)
                    base_receives_by_node[_integer(row, "node_num")] = row
                elif event == "REPLY_TX":
                    base_transmits_by_node[_integer(row, "node_num")] = row

        send = send or completion
        if send is None:
            # A base-only capture still contains the mobile GPS carried by each
            # probe and the complete outward-link reading.  Surface those points
            # immediately; if a mobile capture is loaded later, the normal path
            # above replaces these partial measurements for the same sequences.
            for base_receive in base_receives:
                mobile_gps_lock, mobile_latitude, mobile_longitude = _gps_position(
                    base_receive, "remote"
                )
                base_gps_lock, base_latitude, base_longitude = _gps_position(
                    base_receive, "local"
                )
                forward_rssi_dbm, forward_snr_db = _rx_metrics(base_receive, "local")
                measurements.append(
                    {
                        "session_id": session_id,
                        "sequence": sequence,
                        "epoch_s": _integer(base_receive, "epoch_s"),
                        "mobile_node_num": _integer(base_receive, "peer_num"),
                        "base_node_num": _integer(base_receive, "node_num"),
                        "mobile_gps_lock": mobile_gps_lock,
                        "mobile_latitude": mobile_latitude,
                        "mobile_longitude": mobile_longitude,
                        "mobile_altitude_m": _integer(base_receive, "remote_altitude_m"),
                        "mobile_hdop": _integer(base_receive, "remote_hdop_centi") / 100.0,
                        "mobile_satellites": _integer(base_receive, "remote_satellites"),
                        "base_latitude": base_latitude,
                        "base_longitude": base_longitude,
                        "base_gps_lock": base_gps_lock,
                        "base_altitude_m": _integer(base_receive, "local_altitude_m"),
                        "base_hdop": _integer(base_receive, "local_hdop_centi") / 100.0,
                        "base_satellites": _integer(base_receive, "local_satellites"),
                        "forward_received": True,
                        "forward_rssi_dbm": forward_rssi_dbm,
                        "forward_snr_db": forward_snr_db,
                        # The base knows that it transmitted a reply, but cannot
                        # know whether the mobile received it without that log.
                        "reply_received": None,
                        "base_reply_sent": bool(_integer(base_receive, "reply_sent")),
                        "reverse_rssi_dbm": None,
                        "reverse_snr_db": None,
                        "region": _integer(base_receive, "region"),
                        "modem_preset": _integer(base_receive, "modem_preset"),
                        "frequency_hz": _integer(base_receive, "frequency_hz"),
                        "tx_power_dbm": _integer(base_receive, "tx_power_dbm"),
                        "channel_utilization_pct": _integer(
                            base_receive, "channel_utilization_centi_pct"
                        ) / 100.0,
                        "tx_utilization_pct": _integer(
                            base_receive, "tx_utilization_centi_pct"
                        ) / 100.0,
                    }
                )
            continue
        mobile_node = _integer(send, "node_num")
        base_nodes = set(base_receives_by_node)
        base_nodes.update(mobile_replies_by_peer)
        base_nodes.update(known_base_nodes)
        base_nodes.discard(0)
        if not base_nodes:
            base_nodes = {0}

        mobile_gps_lock, mobile_latitude, mobile_longitude = _gps_position(send, "local")
        send_epoch_s = _integer(send, "epoch_s")
        mobile_altitude_m = _integer(send, "local_altitude_m")
        mobile_hdop = _integer(send, "local_hdop_centi") / 100.0
        mobile_satellites = _integer(send, "local_satellites")
        region = _integer(send, "region")
        modem_preset = _integer(send, "modem_preset")
        frequency_hz = _integer(send, "frequency_hz")
        tx_power_dbm = _integer(send, "tx_power_dbm")
        channel_utilization_pct = _integer(send, "channel_utilization_centi_pct") / 100.0
        tx_utilization_pct = _integer(send, "tx_utilization_centi_pct") / 100.0
        for base_node in sorted(base_nodes):
            base_receive = base_receives_by_node.get(base_node)
            base_transmit = base_transmits_by_node.get(base_node)
            mobile_reply = mobile_replies_by_peer.get(base_node)
            base_source = mobile_reply or base_receive
            base_prefix = "remote" if base_source is mobile_reply else "local"
            forward_source = mobile_reply if mobile_reply and _integer(mobile_reply, "remote_rx_valid") else base_receive
            forward_prefix = "remote" if forward_source is mobile_reply else "local"
            forward_received = base_receive is not None or mobile_reply is not None
            reply_received = mobile_reply is not None
            base_gps_lock, base_latitude, base_longitude = _gps_position(base_source, base_prefix)
            forward_rssi_dbm, forward_snr_db = _rx_metrics(forward_source, forward_prefix)
            reverse_rssi_dbm, reverse_snr_db = _rx_metrics(mobile_reply, "local")
            measurement = {
                "session_id": session_id,
                "sequence": sequence,
                "epoch_s": send_epoch_s,
                "mobile_node_num": mobile_node,
                "base_node_num": base_node,
                "mobile_gps_lock": mobile_gps_lock,
                "mobile_latitude": mobile_latitude,
                "mobile_longitude": mobile_longitude,
                "mobile_altitude_m": mobile_altitude_m,
                "mobile_hdop": mobile_hdop,
                "mobile_satellites": mobile_satellites,
                "base_latitude": base_latitude,
                "base_longitude": base_longitude,
                "base_gps_lock": base_gps_lock,
                "base_altitude_m": _integer(
                    base_source, f"{base_prefix}_altitude_m"
                ),
                "base_hdop": _integer(base_source, f"{base_prefix}_hdop_centi") / 100.0,
                "base_satellites": _integer(base_source, f"{base_prefix}_satellites"),
                "forward_received": forward_received,
                "forward_rssi_dbm": forward_rssi_dbm,
                "forward_snr_db": forward_snr_db,
                "reply_received": reply_received,
                "base_reply_sent": bool(base_transmit or _integer(base_receive, "reply_sent") or reply_received),
                "reverse_rssi_dbm": reverse_rssi_dbm,
                "reverse_snr_db": reverse_snr_db,
                "region": region,
                "modem_preset": modem_preset,
                "frequency_hz": frequency_hz,
                "tx_power_dbm": tx_power_dbm,
                "channel_utilization_pct": channel_utilization_pct,
                "tx_utilization_pct": tx_utilization_pct,
            }
            measurements.append(measurement)
    return measurements


def write_rows(path: str | Path, rows: Iterable[dict[str, object]]) -> int:
    materialized = list(rows)
    if not materialized:
        Path(path).write_text("", encoding="utf-8")
        return 0
    fieldnames = list(materialized[0])
    known = set(fieldnames)
    for row in materialized[1:]:
        for field in row:
            if field not in known:
                known.add(field)
                fieldnames.append(field)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(materialized)
    return len(materialized)
