from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

SURVEY_SCHEMA = "1"
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
    "local_rx_valid",
    "local_rx_rssi_dbm",
    "local_rx_snr_centi_db",
    "remote_rx_valid",
    "remote_rx_rssi_dbm",
    "remote_rx_snr_centi_db",
}


class SurveyLogError(ValueError):
    pass


def read_survey_log(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = sorted(SURVEY_REQUIRED_FIELDS - fields)
        if missing:
            raise SurveyLogError(f"{path} is not a MeshLab survey log; missing: {', '.join(missing)}")
        rows = list(reader)
    invalid = sorted({row.get("schema", "") for row in rows if row.get("schema") != SURVEY_SCHEMA})
    if invalid:
        raise SurveyLogError(f"{path} uses unsupported survey schema values: {', '.join(invalid)}")
    return rows


def _integer(row: dict[str, str] | None, field: str, default: int = 0) -> int:
    if not row:
        return default
    try:
        return int(row.get(field, "") or default)
    except ValueError:
        return default


def _gps_degrees(row: dict[str, str] | None, prefix: str, axis: str) -> float | None:
    if not row or not _integer(row, f"{prefix}_gps_lock"):
        return None
    return _integer(row, f"{prefix}_{axis}_i") * 1e-7


def _rx_value(row: dict[str, str] | None, prefix: str, field: str, scale: float = 1.0) -> float | None:
    if not row or not _integer(row, f"{prefix}_rx_valid"):
        return None
    return _integer(row, f"{prefix}_rx_{field}") / scale


def merge_survey_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, object]]:
    rows = list(rows)
    known_base_nodes = {_integer(row, "node_num") for row in rows if row.get("role") == "base"}
    known_base_nodes.discard(0)
    grouped: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        sequence = _integer(row, "sequence")
        if sequence:
            grouped[(_integer(row, "session_id"), sequence)].append(row)

    measurements: list[dict[str, object]] = []
    for (session_id, sequence), group in sorted(grouped.items()):
        sends = [row for row in group if row.get("role") == "mobile" and row.get("event") == "SEND"]
        if not sends:
            continue
        send = sends[-1]
        mobile_node = _integer(send, "node_num")
        base_receives = [row for row in group if row.get("role") == "base" and row.get("event") == "PROBE_RX"]
        mobile_replies = [row for row in group if row.get("role") == "mobile" and row.get("event") == "REPLY_RX"]

        base_nodes = {_integer(row, "node_num") for row in base_receives}
        base_nodes.update(_integer(row, "peer_num") for row in mobile_replies)
        base_nodes.update(known_base_nodes)
        base_nodes.discard(0)
        if not base_nodes:
            base_nodes = {0}

        for base_node in sorted(base_nodes):
            base_receive = next((row for row in reversed(base_receives) if _integer(row, "node_num") == base_node), None)
            mobile_reply = next((row for row in reversed(mobile_replies) if _integer(row, "peer_num") == base_node), None)
            base_source = mobile_reply or base_receive
            forward_source = mobile_reply if mobile_reply and _integer(mobile_reply, "remote_rx_valid") else base_receive
            forward_prefix = "remote" if forward_source is mobile_reply else "local"
            forward_received = base_receive is not None or mobile_reply is not None
            reply_received = mobile_reply is not None
            measurement = {
                "session_id": session_id,
                "sequence": sequence,
                "epoch_s": _integer(send, "epoch_s"),
                "mobile_node_num": mobile_node,
                "base_node_num": base_node,
                "mobile_gps_lock": bool(_integer(send, "local_gps_lock")),
                "mobile_latitude": _gps_degrees(send, "local", "latitude"),
                "mobile_longitude": _gps_degrees(send, "local", "longitude"),
                "mobile_altitude_m": _integer(send, "local_altitude_m"),
                "mobile_pdop": _integer(send, "local_pdop_centi") / 100.0,
                "mobile_satellites": _integer(send, "local_satellites"),
                "base_latitude": _gps_degrees(base_source, "remote" if base_source is mobile_reply else "local", "latitude"),
                "base_longitude": _gps_degrees(base_source, "remote" if base_source is mobile_reply else "local", "longitude"),
                "base_gps_lock": bool(
                    _integer(base_source, "remote_gps_lock" if base_source is mobile_reply else "local_gps_lock")
                ),
                "base_altitude_m": _integer(
                    base_source, "remote_altitude_m" if base_source is mobile_reply else "local_altitude_m"
                ),
                "base_pdop": _integer(
                    base_source, "remote_pdop_centi" if base_source is mobile_reply else "local_pdop_centi"
                )
                / 100.0,
                "base_satellites": _integer(
                    base_source, "remote_satellites" if base_source is mobile_reply else "local_satellites"
                ),
                "forward_received": forward_received,
                "forward_rssi_dbm": _rx_value(forward_source, forward_prefix, "rssi_dbm"),
                "forward_snr_db": _rx_value(forward_source, forward_prefix, "snr_centi_db", 100.0),
                "reply_received": reply_received,
                "reverse_rssi_dbm": _rx_value(mobile_reply, "local", "rssi_dbm"),
                "reverse_snr_db": _rx_value(mobile_reply, "local", "snr_centi_db", 100.0),
                "region": _integer(send, "region"),
                "modem_preset": _integer(send, "modem_preset"),
                "frequency_hz": _integer(send, "frequency_hz"),
                "tx_power_dbm": _integer(send, "tx_power_dbm"),
                "channel_utilization_pct": _integer(send, "channel_utilization_centi_pct") / 100.0,
                "tx_utilization_pct": _integer(send, "tx_utilization_centi_pct") / 100.0,
            }
            measurements.append(measurement)
    return measurements


def write_rows(path: str | Path, rows: Iterable[dict[str, object]]) -> int:
    materialized = list(rows)
    if not materialized:
        Path(path).write_text("", encoding="utf-8")
        return 0
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(materialized[0]))
        writer.writeheader()
        writer.writerows(materialized)
    return len(materialized)
