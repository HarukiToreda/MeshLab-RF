from __future__ import annotations

import argparse
import binascii
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from serial import Serial
from serial.tools import list_ports

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mesh_simulator.survey import (
    SURVEY_RECORD_SIZE,
    decode_survey_records,
    merge_survey_rows,
    write_rows,
)

T114_USB_IDS = {(0x239A, 0x4405), (0x239A, 0x0029), (0x239A, 0x002A), (0x2886, 0x1667)}
INFO_PATTERN = re.compile(r"^MESHLAB_INFO,1,(MOBILE|BASE),([0-9A-Fa-f]{16}),(\d+),(\d+)$")
BEGIN_PATTERN = re.compile(r"^MESHLAB_BEGIN,1,(MOBILE|BASE),([0-9A-Fa-f]{16}),(\d+),(\d+)$")
END_PATTERN = re.compile(r"^MESHLAB_END,([0-9A-Fa-f]{8})$")


@dataclass(frozen=True)
class DeviceInfo:
    port: str
    role: str
    node_id: int
    slots: int
    record_size: int


def candidate_ports() -> list[str]:
    matches = []
    for port in list_ports.comports():
        description = " ".join(filter(None, (port.product, port.description, port.manufacturer))).lower()
        if (port.vid, port.pid) in T114_USB_IDS or "t114 signal tester" in description:
            matches.append(port.device)
    return matches


def read_matching_line(serial: Serial, pattern: re.Pattern[str], timeout: float = 8.0) -> re.Match[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = serial.readline().decode("ascii", errors="ignore").strip()
        match = pattern.match(line)
        if match:
            return match
    raise TimeoutError(f"no standalone MeshLab response from {serial.port}")


def query_device(port: str) -> DeviceInfo:
    with Serial(port, 115200, timeout=0.25, write_timeout=3) as serial:
        time.sleep(0.4)
        serial.reset_input_buffer()
        serial.write(b"MESHLAB_INFO\n")
        match = read_matching_line(serial, INFO_PATTERN)
    role, node_hex, slots, record_size = match.groups()
    if int(record_size) != SURVEY_RECORD_SIZE:
        raise RuntimeError(f"{port} uses unsupported record size {record_size}")
    return DeviceInfo(port, role.lower(), int(node_hex, 16), int(slots), int(record_size))


def read_exact(serial: Serial, size: int, timeout: float = 60.0) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while len(output) < size:
        chunk = serial.read(min(4096, size - len(output)))
        if chunk:
            output.extend(chunk)
            deadline = time.monotonic() + timeout
        elif time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {len(output):,} of {size:,} bytes from {serial.port}")
    return bytes(output)


def download_device(info: DeviceInfo, destination: Path) -> tuple[Path, list[dict[str, str]]]:
    print(f"Downloading {info.role} log from {info.port}...", flush=True)
    with Serial(info.port, 115200, timeout=0.25, write_timeout=3) as serial:
        time.sleep(0.4)
        serial.reset_input_buffer()
        serial.write(b"MESHLAB_DUMP\n")
        match = read_matching_line(serial, BEGIN_PATTERN)
        role, node_hex, slots, record_size = match.groups()
        if role.lower() != info.role or int(node_hex, 16) != info.node_id:
            raise RuntimeError(f"{info.port} identity changed during download")
        byte_count = int(slots) * int(record_size)
        raw = read_exact(serial, byte_count)
        end = read_matching_line(serial, END_PATTERN)
        expected_crc = int(end.group(1), 16)
        actual_crc = binascii.crc32(raw) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise RuntimeError(
                f"{info.port} dump CRC mismatch: expected {expected_crc:08X}, received {actual_crc:08X}"
            )

    stem = f"{info.role}-node-{info.node_id:016x}"
    binary_path = destination / f"{stem}.bin"
    binary_path.write_bytes(raw)
    rows, invalid = decode_survey_records(raw)
    csv_path = destination / f"{stem}.csv"
    write_rows(csv_path, rows)
    print(f"  {len(rows):,} valid records, {invalid:,} damaged slots -> {csv_path}")
    return binary_path, rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and merge logs from two standalone MeshLab T114 signal testers."
    )
    parser.add_argument("--ports", nargs="+", help="Both serial ports, for example --ports COM7 COM8")
    parser.add_argument("--output-dir", type=Path, help="Destination directory (default: survey-data/<timestamp>)")
    args = parser.parse_args()

    ports = args.ports or candidate_ports()
    if len(ports) != 2:
        parser.error(f"expected exactly two T114 ports; found {len(ports)} ({', '.join(ports) or 'none'})")
    devices = [query_device(port) for port in ports]
    roles = sorted(device.role for device in devices)
    if roles != ["base", "mobile"]:
        parser.error(f"expected one base and one mobile firmware; found {', '.join(roles)}")

    destination = args.output_dir or Path("survey-data") / datetime.now().strftime("%Y%m%d-%H%M%S")
    destination.mkdir(parents=True, exist_ok=True)
    raw_rows: list[dict[str, str]] = []
    for device in devices:
        _, rows = download_device(device, destination)
        raw_rows.extend(rows)
    if not raw_rows:
        raise RuntimeError("the two survey logs contain no valid records")

    combined_path = destination / "combined-device-log.csv"
    with open(combined_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(raw_rows[0]))
        writer.writeheader()
        writer.writerows(raw_rows)

    measurements = merge_survey_rows(raw_rows)
    measurements_path = destination / "measurements.csv"
    write_rows(measurements_path, measurements)
    forward = sum(bool(row["forward_received"]) for row in measurements)
    replies = sum(bool(row["reply_received"]) for row in measurements)
    print(f"Merged {len(measurements):,} probes: {forward:,} reached base, {replies:,} replies returned")
    print(f"Calibration input: {measurements_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
