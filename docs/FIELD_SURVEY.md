# Standalone T114 building-loss survey

This project includes a purpose-built signal tester for two Heltec Mesh Node T114 radios. It is not Meshtastic firmware and does not contain a mesh stack, routing, Bluetooth, telemetry, a node database, or Meshtastic background traffic.

The source is in [`t114_signal_tester`](../t114_signal_tester). Two builds are produced from the same code:

- `heltec-t114-signal-mobile.uf2` sends a direct LoRa probe after each fresh GPS location, no faster than every five seconds.
- `heltec-t114-signal-base.uf2` listens at a fixed location and immediately returns a direct LoRa reply.

Both radios log every relevant event to a reserved 1 MB region of their external QSPI flash. The compact records are protected by CRC-32 and survive reboots. At the five-second interval, the region holds roughly 4,000 complete probe/reply exchanges per radio, or more than five hours of continuous walking.

## Radio profile

The checked-in build matches MeshLab RF's current US915 LongFast default:

- 906.875 MHz
- 250 kHz bandwidth
- spreading factor 11
- coding rate 4/5
- 22 dBm SX1262 output
- private LoRa sync word `0x12`

These are standalone tester packets, not Meshtastic packets. Do not transmit this build outside a region where this profile is legal. For another region, change the constants in `t114_signal_tester/include/survey_config.h` and rebuild both roles.

On its first standalone boot, the tester initializes only the final 1 MB of the T114's 2 MB external QSPI flash for survey records. It does not repeatedly erase that area. Reflashing or rebooting does not clear a valid survey log.

## Display behavior

The mobile display reports every successful probe transmission, every reply with outward and return RSSI/SNR, and every reply timeout. The base display reports every received probe with RSSI/SNR, whether its reply was sent, and whether the base had a GPS lock.

The separate logs are essential. A mobile timeout alone cannot distinguish an outward probe loss from a return-reply loss. The base log resolves that ambiguity, and a total loss remains a useful censored measurement rather than disappearing from the dataset.

## Build and flash

Build both images from the project root:

```powershell
cd t114_signal_tester
python -m platformio run
```

The ready-to-flash UF2 files are also copied to `artifacts/t114-signal-tester`. Put each T114 into its UF2 bootloader, then copy the correctly labeled image to that board. Flash one mobile and one base.

## Collect useful calibration data

1. Put the base at a fixed, accurately known point with a clear view of the sky and wait for its GPS lock.
2. Wait for the mobile GPS lock before beginning the route.
3. Keep both antennas in a consistent orientation and height. Keep the walking radio in a consistent position relative to your body.
4. Record open line-of-sight paths first. These establish the radio, antenna, and local-noise baseline.
5. Walk paths whose direct lines cross the neighborhood's buildings at several distances and angles. Repeat routes in both directions at least three times.

This measures local effective excess loss, not a universal loss for every building. Wall materials, windows, foliage, antenna placement, vehicles, multipath, weather, and local noise all contribute. The later calibration will fit the clear-path bias first, then compare measured link loss with the simulator's building intersections.

## Extract both radios

After the walk, connect **both T114s by USB**. From the repository root run:

```powershell
python tools\extract_survey.py
```

If automatic USB detection does not find exactly two testers, specify both ports:

```powershell
python tools\extract_survey.py --ports COM7 COM8
```

The extractor verifies that one board is mobile and one is base, downloads each QSPI log with an end-to-end CRC, validates every individual record, and writes a timestamped folder under `survey-data`. Keep the entire folder. It contains:

- an untouched binary dump and decoded CSV from each radio;
- `combined-device-log.csv`, containing every valid device event;
- `measurements.csv`, containing one joined calibration record per probe.

`measurements.csv` explicitly separates `forward_received` from `reply_received` and contains GPS lock/position, HDOP, satellites, RSSI, SNR, radio parameters, and packet-loss outcomes for the two directions.
