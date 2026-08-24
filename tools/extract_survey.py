from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mesh_simulator.survey_device import (
    candidate_ports,
    default_export_destination,
    discover_devices,
    export_devices,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download one or two standalone MeshLab survey-node logs and merge available directions."
    )
    parser.add_argument(
        "--ports",
        nargs="+",
        help="One or two serial ports, for example --ports COM7 or --ports COM7 COM8",
    )
    parser.add_argument("--output-dir", type=Path, help="Destination directory (default: survey-data/<timestamp>)")
    args = parser.parse_args()

    ports = args.ports or candidate_ports()
    if not 1 <= len(ports) <= 2:
        parser.error(f"expected one or two survey-node ports; found {len(ports)} ({', '.join(ports) or 'none'})")
    devices = discover_devices(ports)

    def progress(message: str, _fraction: float | None) -> None:
        print(message, flush=True)

    result = export_devices(devices, args.output_dir or default_export_destination(), progress)
    forward = sum(str(row["forward_received"]).lower() in {"1", "true"} for row in result.measurements)
    replies = sum(str(row["reply_received"]).lower() in {"1", "true"} for row in result.measurements)
    print(
        f"Exported {', '.join(result.roles)}: {len(result.measurements):,} available probes, "
        f"{forward:,} reached base, {replies:,} replies returned"
    )
    print(f"Calibration input: {result.measurements_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
