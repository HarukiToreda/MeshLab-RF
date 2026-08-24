from __future__ import annotations

import binascii
import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from serial import Serial
from serial.tools import list_ports

from .survey import (
    DEFAULT_RADIO_PROFILE,
    SURVEY_RECORD_SIZE,
    SURVEY_RECORD_V1_SIZE,
    decode_survey_records,
    merge_survey_rows,
    write_rows,
)

# Known boards make the command-line fallback convenient, but the GUI permits
# selecting any serial port so future survey-node hardware is not excluded.
KNOWN_SURVEY_USB_IDS = {(0x239A, 0x4405), (0x239A, 0x0029), (0x239A, 0x002A), (0x2886, 0x1667)}
DEVICE_PATTERN_SUFFIX = r",(\d+),(\d+)(?:,(\d+),(\d+),(\d+),(\d+),(-?\d+))?$"
INFO_PATTERN = re.compile(r"^MESHLAB_INFO,([12]),(MOBILE|BASE),([0-9A-Fa-f]{16})" + DEVICE_PATTERN_SUFFIX)
BEGIN_PATTERN = re.compile(r"^MESHLAB_BEGIN,([12]),(MOBILE|BASE),([0-9A-Fa-f]{16})" + DEVICE_PATTERN_SUFFIX)
END_PATTERN = re.compile(r"^MESHLAB_END,([0-9A-Fa-f]{8})$")
ProgressCallback = Callable[[str, float | None], None]


@dataclass(frozen=True)
class DeviceInfo:
    port: str
    version: int
    role: str
    node_id: int
    slots: int
    record_size: int
    radio_profile: dict[str, int]


@dataclass(frozen=True)
class DeviceDownload:
    info: DeviceInfo
    binary_path: Path
    csv_path: Path
    valid_records: int
    damaged_records: int
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class DeviceCapture:
    info: DeviceInfo
    raw: bytes
    valid_records: int
    damaged_records: int
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class SurveyExport:
    destination: Path
    devices: tuple[DeviceDownload, ...]
    roles: tuple[str, ...]
    measurements: list[dict[str, object]]
    combined_path: Path
    measurements_path: Path


def candidate_ports() -> list[str]:
    matches: list[str] = []
    for port in list_ports.comports():
        description = " ".join(filter(None, (port.product, port.description, port.manufacturer))).lower()
        if (
            (port.vid, port.pid) in KNOWN_SURVEY_USB_IDS
            or "signal tester" in description
            or "survey node" in description
        ):
            matches.append(port.device)
    return matches


def read_matching_line(serial: Serial, pattern: re.Pattern[str], timeout: float = 8.0) -> re.Match[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = serial.readline().decode("ascii", errors="ignore").strip()
        match = pattern.match(line)
        if match:
            return match
    raise TimeoutError(f"no MeshLab survey-firmware response from {serial.port}")


def query_device(port: str) -> DeviceInfo:
    with Serial(port, 115200, timeout=0.25, write_timeout=3) as serial:
        serial.dtr = True
        time.sleep(0.8)
        last_error: TimeoutError | None = None
        for _attempt in range(3):
            serial.reset_input_buffer()
            serial.write(b"MESHLAB_INFO\r\n")
            serial.flush()
            try:
                match = read_matching_line(serial, INFO_PATTERN, timeout=3.0)
                break
            except TimeoutError as error:
                last_error = error
                time.sleep(0.4)
        else:
            assert last_error is not None
            raise last_error
    version, role, node_hex, slots, record_size, frequency, bandwidth, spreading, coding, power = match.groups()
    version_i, record_size_i = int(version), int(record_size)
    if (version_i, record_size_i) not in {(1, SURVEY_RECORD_V1_SIZE), (2, SURVEY_RECORD_SIZE)}:
        raise RuntimeError(f"{port} uses unsupported survey format v{version_i}/{record_size_i} bytes")
    profile = dict(DEFAULT_RADIO_PROFILE)
    if frequency is not None:
        profile.update(
            frequency_hz=int(frequency),
            bandwidth_khz=int(bandwidth),
            spreading_factor=int(spreading),
            coding_rate=int(coding),
            tx_power_dbm=int(power),
        )
    return DeviceInfo(port, version_i, role.lower(), int(node_hex, 16), int(slots), record_size_i, profile)


def discover_devices(ports: Iterable[str] | None = None) -> list[DeviceInfo]:
    selected = list(ports) if ports is not None else candidate_ports()
    return [query_device(port) for port in selected]


def validate_device_pair(devices: Iterable[DeviceInfo]) -> tuple[DeviceInfo, DeviceInfo]:
    pair = tuple(devices)
    if len(pair) != 2:
        raise RuntimeError(f"expected exactly two survey nodes; found {len(pair)}")
    by_role = {device.role: device for device in pair}
    if set(by_role) != {"mobile", "base"}:
        roles = ", ".join(device.role for device in pair)
        raise RuntimeError(f"expected one mobile and one base tester; found {roles}")
    return by_role["mobile"], by_role["base"]


def validate_export_devices(devices: Iterable[DeviceInfo]) -> tuple[DeviceInfo, ...]:
    selected = tuple(devices)
    if not 1 <= len(selected) <= 2:
        raise RuntimeError(f"expected one or two survey nodes; found {len(selected)}")
    roles = [device.role for device in selected]
    if any(role not in {"mobile", "base"} for role in roles):
        raise RuntimeError(f"unsupported survey-node role: {', '.join(roles)}")
    if len(set(roles)) != len(roles):
        raise RuntimeError(f"select at most one node for each role; found {', '.join(roles)}")
    if len(selected) == 2:
        mobile, base = validate_device_pair(selected)
        return mobile, base
    return selected


def read_exact(
    serial: Serial,
    size: int,
    timeout: float = 60.0,
    progress: Callable[[int, int], None] | None = None,
) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while len(output) < size:
        chunk = serial.read(min(4096, size - len(output)))
        if chunk:
            output.extend(chunk)
            deadline = time.monotonic() + timeout
            if progress:
                progress(len(output), size)
        elif time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {len(output):,} of {size:,} bytes from {serial.port}")
    return bytes(output)


def capture_device(
    info: DeviceInfo,
    progress: ProgressCallback | None = None,
) -> DeviceCapture:
    if progress:
        progress(f"Downloading {info.role} from {info.port}", 0.0)
    with Serial(info.port, 115200, timeout=0.25, write_timeout=3) as serial:
        serial.dtr = True
        time.sleep(0.8)
        serial.reset_input_buffer()
        serial.write(b"MESHLAB_DUMP\r\n")
        serial.flush()
        match = read_matching_line(serial, BEGIN_PATTERN)
        version, role, node_hex, slots, record_size, *_ = match.groups()
        if int(version) != info.version or role.lower() != info.role or int(node_hex, 16) != info.node_id:
            raise RuntimeError(f"{info.port} identity changed during download")
        byte_count = int(slots) * int(record_size)

        def report(received: int, total: int) -> None:
            if progress:
                fraction = received / total if total else 1.0
                progress(f"Downloading {info.role}: {received:,} / {total:,} bytes", fraction)

        raw = read_exact(serial, byte_count, progress=report)
        end = read_matching_line(serial, END_PATTERN)
        expected_crc = int(end.group(1), 16)
        actual_crc = binascii.crc32(raw) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise RuntimeError(
                f"{info.port} dump CRC mismatch: expected {expected_crc:08X}, received {actual_crc:08X}"
            )

    rows, invalid = decode_survey_records(raw, info.record_size, info.radio_profile)
    if progress:
        progress(f"Validated {len(rows):,} {info.role} records", 1.0)
    return DeviceCapture(info, raw, len(rows), invalid, rows)


def save_capture(capture: DeviceCapture, destination: Path) -> DeviceDownload:
    info = capture.info
    stem = f"{info.role}-node-{info.node_id:016x}"
    binary_path = destination / f"{stem}.bin"
    binary_path.write_bytes(capture.raw)
    csv_path = destination / f"{stem}.csv"
    write_rows(csv_path, capture.rows)
    (destination / f"{stem}.json").write_text(
        json.dumps(
            {
                "format_version": info.version,
                "role": info.role,
                "node_id": f"{info.node_id:016x}",
                "record_size": info.record_size,
                "records": info.slots,
                "damaged_records": capture.damaged_records,
                **info.radio_profile,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return DeviceDownload(
        info,
        binary_path,
        csv_path,
        capture.valid_records,
        capture.damaged_records,
        capture.rows,
    )


def download_device(
    info: DeviceInfo,
    destination: Path,
    progress: ProgressCallback | None = None,
) -> DeviceDownload:
    return save_capture(capture_device(info, progress), destination)


def save_captures(
    captures: Iterable[DeviceCapture],
    destination: str | Path,
    progress: ProgressCallback | None = None,
) -> SurveyExport:
    selected = tuple(captures)
    validate_export_devices(capture.info for capture in selected)
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    downloads = tuple(save_capture(capture, output) for capture in selected)
    if progress:
        progress(f"Saved {len(downloads)} captured survey node{'s' if len(downloads) != 1 else ''}", 0.8)
    return _finish_export(output, downloads, progress)


def _finish_export(
    output: Path,
    downloads: tuple[DeviceDownload, ...],
    progress: ProgressCallback | None = None,
) -> SurveyExport:
    raw_rows: list[dict[str, str]] = [row for download in downloads for row in download.rows]
    current_csv_paths = {download.csv_path.resolve() for download in downloads}
    for csv_path in sorted(output.glob("*-node-*.csv")):
        if csv_path.resolve() in current_csv_paths:
            continue
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            raw_rows.extend(csv.DictReader(handle))
    if not raw_rows:
        raise RuntimeError("the selected survey log contains no valid records")
    combined_path = output / "combined-device-log.csv"
    write_rows(combined_path, raw_rows)
    measurements = merge_survey_rows(raw_rows)
    measurements_path = output / "measurements.csv"
    write_rows(measurements_path, measurements)
    roles = sorted({str(row.get("role", "")).lower() for row in raw_rows} - {""})
    (output / "survey-export.json").write_text(
        json.dumps(
            {
                "complete_pair": set(roles) == {"mobile", "base"},
                "roles": roles,
                "measurements": len(measurements),
                "note": (
                    "Paired mobile/base export; direction-specific loss can be resolved."
                    if set(roles) == {"mobile", "base"}
                    else "Single-node export retained for recovery or later pairing; loss direction may be unresolved."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if progress:
        progress(f"Created {len(measurements):,} available measurements", 1.0)
    return SurveyExport(output, downloads, tuple(roles), measurements, combined_path, measurements_path)


def export_devices(
    devices: Iterable[DeviceInfo],
    destination: str | Path,
    progress: ProgressCallback | None = None,
) -> SurveyExport:
    selected = validate_export_devices(devices)
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    downloads = tuple(download_device(device, output, progress) for device in selected)
    return _finish_export(output, downloads, progress)


def export_device_pair(
    devices: Iterable[DeviceInfo],
    destination: str | Path,
    progress: ProgressCallback | None = None,
) -> SurveyExport:
    pair = validate_device_pair(devices)
    return export_devices(pair, destination, progress)


def default_export_destination(parent: str | Path = "survey-data") -> Path:
    return Path(parent) / datetime.now().strftime("%Y%m%d-%H%M%S")


def read_measurements(path: str | Path) -> list[dict[str, object]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows and Path(path).name.lower() == "measurements.csv":
        return []
    required = {
        "sequence",
        "mobile_latitude",
        "mobile_longitude",
        "forward_received",
        "forward_rssi_dbm",
        "reply_received",
        "reverse_rssi_dbm",
    }
    fields = set(rows[0]) if rows else set()
    missing = sorted(required - fields)
    if missing:
        raise ValueError(f"not a MeshLab measurements.csv file; missing {', '.join(missing)}")
    return rows
