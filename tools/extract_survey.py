from __future__ import annotations

import argparse
import binascii
import csv
import queue
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from pubsub import pub
from serial.tools import list_ports

from meshtastic.protobuf import mesh_pb2, xmodem_pb2
from meshtastic.serial_interface import SerialInterface

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mesh_simulator.survey import merge_survey_rows, read_survey_log, write_rows

SURVEY_DEVICE_PATH = "/static/meshlab-survey.csv"
T114_USB_IDS = {(0x239A, 0x4405), (0x239A, 0x0029), (0x239A, 0x002A), (0x2886, 0x1667)}


def crc16_ccitt(data: bytes) -> int:
    return binascii.crc_hqx(data, 0)


def discover_t114_ports() -> list[str]:
    matches = []
    for port in list_ports.comports():
        identity = (port.vid, port.pid)
        description = " ".join(filter(None, (port.product, port.description, port.manufacturer))).lower()
        if identity in T114_USB_IDS or "t114" in description or "ht-n5262" in description:
            matches.append(port.device)
    return matches


class FileDownload:
    def __init__(self, interface: SerialInterface, timeout: float = 15.0) -> None:
        self.interface = interface
        self.timeout = timeout
        self.incoming: queue.Queue[xmodem_pb2.XModem] = queue.Queue()

    def _on_packet(self, packet: xmodem_pb2.XModem, interface: SerialInterface) -> None:
        if interface is self.interface:
            copy = xmodem_pb2.XModem()
            copy.CopyFrom(packet)
            self.incoming.put(copy)

    def _send(self, packet: xmodem_pb2.XModem) -> None:
        envelope = mesh_pb2.ToRadio()
        envelope.xmodemPacket.CopyFrom(packet)
        self.interface._sendToRadio(envelope)

    def download(self, remote_path: str) -> bytes:
        pub.subscribe(self._on_packet, "meshtastic.xmodempacket")
        try:
            request = xmodem_pb2.XModem(control=xmodem_pb2.XModem.STX, seq=0, buffer=remote_path.encode())
            self._send(request)
            expected_sequence = 1
            output = bytearray()
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out downloading {remote_path}")
                try:
                    packet = self.incoming.get(timeout=remaining)
                except queue.Empty as error:
                    raise TimeoutError(f"timed out downloading {remote_path}") from error
                if packet.control == xmodem_pb2.XModem.NAK:
                    raise FileNotFoundError(f"device could not open {remote_path}")
                if packet.control == xmodem_pb2.XModem.CAN:
                    raise OSError(f"device cancelled transfer of {remote_path}")
                if packet.control == xmodem_pb2.XModem.EOT:
                    return bytes(output)
                if packet.control != xmodem_pb2.XModem.SOH:
                    continue
                data = bytes(packet.buffer)
                if packet.seq != expected_sequence or crc16_ccitt(data) != packet.crc16:
                    self._send(xmodem_pb2.XModem(control=xmodem_pb2.XModem.NAK, seq=expected_sequence))
                    deadline = time.monotonic() + self.timeout
                    continue
                output.extend(data)
                self._send(xmodem_pb2.XModem(control=xmodem_pb2.XModem.ACK, seq=packet.seq))
                expected_sequence += 1
                deadline = time.monotonic() + self.timeout
        finally:
            pub.unsubscribe(self._on_packet, "meshtastic.xmodempacket")


def safe_port_name(port: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", port).strip("_") or "device"


def extract_port(port: str, destination: Path) -> Path:
    print(f"Connecting to {port}...", flush=True)
    interface = SerialInterface(devPath=port, noNodes=True, timeout=60)
    try:
        node_num = int(interface.myInfo.my_node_num)
        data = FileDownload(interface).download(SURVEY_DEVICE_PATH)
    finally:
        interface.close()
    output = destination / f"{safe_port_name(port)}-node-{node_num:08x}.csv"
    output.write_bytes(data)
    print(f"  downloaded {len(data):,} bytes to {output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and merge MeshLab RF survey logs from two T114 nodes.")
    parser.add_argument("--ports", nargs="+", help="Both serial ports, for example --ports COM7 COM8")
    parser.add_argument("--output-dir", type=Path, help="Destination directory (default: survey-data/<timestamp>)")
    args = parser.parse_args()

    ports = args.ports or discover_t114_ports()
    if len(ports) != 2:
        parser.error(f"expected exactly two T114 ports; found {len(ports)} ({', '.join(ports) or 'none'})")
    destination = args.output_dir or Path("survey-data") / datetime.now().strftime("%Y%m%d-%H%M%S")
    destination.mkdir(parents=True, exist_ok=True)

    raw_paths = [extract_port(port, destination) for port in ports]
    raw_rows = []
    for path in raw_paths:
        raw_rows.extend(read_survey_log(path))

    if not raw_rows:
        raise RuntimeError("the two survey logs contain no rows")

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
