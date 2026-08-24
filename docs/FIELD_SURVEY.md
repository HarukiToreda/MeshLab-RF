# Standalone T114 building-loss survey

This project includes a purpose-built signal tester for two Heltec Mesh Node T114 radios. It is not Meshtastic firmware and does not contain a mesh stack, routing, Bluetooth, telemetry, a node database, or Meshtastic background traffic.

The source is in [`t114_signal_tester`](../t114_signal_tester). Two builds are produced from the same code:

- `heltec-t114-signal-mobile.uf2` sends a direct LoRa probe from each trusted, meaningfully changed GPS location, no faster than every five seconds.
- `heltec-t114-signal-base.uf2` listens at a fixed location and immediately returns a direct LoRa reply.

Both radios log to a linker-reserved 640 KB region of the nRF52840's internal 1 MB flash; this design does not require an external flash chip. Version 2 uses one compact 80-byte record per completed exchange on each device, protected by CRC-32. After its 4 KB header, the region holds 8,140 records, approximately 11.3 hours at the five-second probe interval. Logs survive stopping, shutdown, restart, and normal UF2 firmware reflashing until you explicitly wipe them.

## Radio profile

The checked-in build matches MeshLab RF's current US915 LongFast default:

- 906.875 MHz
- 250 kHz bandwidth
- spreading factor 11
- coding rate 4/5
- 22 dBm SX1262 output
- private LoRa sync word `0x12`

These are standalone tester packets, not Meshtastic packets. Do not transmit this build outside a region where this profile is legal. For another region, change the constants in `t114_signal_tester/include/survey_config.h` and rebuild both roles.

The custom linker layout limits the application to `0x26000..0x4CFFF` and reserves `0x4D000..0xECFFF` exclusively for logs. Firmware growth cannot enter the log partition: an oversized build fails at link time. Each compact record is programmed once into erased space, avoiding a page erase for every sample. Erasure happens only when `WIPE ALL LOGS` is confirmed.

The first installation of the internal-flash build may show `LOG NEEDS WIPE` because pages in the newly reserved range can still contain bytes from older, larger firmware. Confirm `WIPE ALL LOGS` once to initialize the partition. Subsequent compatible UF2 updates leave valid survey logs intact.

## Button menu and logging

Logging starts paused after every boot so a setup period does not consume log space. Use the T114 user button on both radios:

1. Tap once to open the popup menu over the current survey screen.
2. Tap to move the highlight through `START / APPEND` or `STOP LOGGING`, `LOG STORAGE`, `SOUND: ON/MUTED`, `WIPE ALL LOGS`, `RESTART DEVICE`, `POWER OFF`, and `EXIT MENU` as applicable. The base identifies sound as always silent.
3. Hold for 500 ms to select the displayed item, matching Meshtastic's screened-device timing. The action registers at the threshold while the button is still held; a short click registers on release.

`WIPE ALL LOGS` requires a second hold to confirm. Wiping is permanent and leaves logging paused. Extract the two logs first if they contain a walk you need.

`LOG STORAGE` shows records used, the 8,140-record maximum, records still free, and used/available data KB. The normal footer also shows `Lused/total`, so capacity remains visible while collecting data. `SOUND: ON/MUTED` controls the mobile's send and receive pings for the current power session; the base is always silent.

`POWER OFF` also requires a second hold. It stops logging, sleeps the LoRa radio and GPS, turns off the display, and enters nRF52840 system-off. Press the same user button to turn the tester back on; it restarts with logging paused and the stored log intact. `RESTART DEVICE` restarts without wiping the log.

Start logging on the base, then start logging on the mobile when both have the desired GPS status. Stop logging on each at the end of the walk. `START / APPEND` preserves existing version 2 records and creates a new session; it does not erase earlier walks.

## Display behavior

The mobile display reports every successful probe transmission, every reply with outward and return RSSI/SNR, and every reply timeout. Its pin-33 buzzer makes one 35 ms ping after a successful send and one after a received reply. The base is silent. The base display reports every received probe with RSSI/SNR, whether its reply was sent, and its current GPS coordinates when locked.

Both displays include a battery gauge, logging state (`R` for running or `P` for paused), and used/total log slots. The battery percentage is an open-circuit voltage estimate and may move under load.

The GPS UART is checked continuously. A coordinate is trusted only after five consecutive fresh fixes with at least six satellites, HDOP at or below 2.00, speed no greater than 15 km/h, and no implausible position jump. The mobile sends the first trusted point, then requires at least five meters of movement and the five-second interval before another probe. Stationary GPS jitter therefore does not create repeated samples. `GPS UART NO DATA`, `WAITING FOR GPS`, `GPS QUALITY LOW`, `GPS STABILIZING`, and `WAITING FOR MOVEMENT` identify the exact reason no packet is being sent. Both builds use the T114 reference wiring: CPU RX P1.5 (Arduino 37), CPU TX P1.7 (Arduino 39), GPS standby P1.2 (34), and peripheral power P0.21 (21).

The separate logs are essential. A mobile timeout alone cannot distinguish an outward probe loss from a return-reply loss. The base log resolves that ambiguity, and a total loss remains a useful censored measurement rather than disappearing from the dataset.

## Build and flash

Build both images from the project root:

```powershell
cd t114_signal_tester
python -m platformio run
```

Checked-in ready-to-flash UF2 files are in `artifacts/t114-signal-tester`. Put each T114 into its UF2 bootloader, then copy the correctly labeled image to that board. Flash one mobile and one base. Rebuilding creates fresh UF2 files under `t114_signal_tester/.pio/build`; copy them into `artifacts` only when intentionally refreshing the checked-in binaries.

## Collect useful calibration data

1. Put the base at a fixed, accurately known point with a clear view of the sky and wait for its GPS lock.
2. Start logging on the base with the button menu.
3. Wait for the mobile GPS lock, then start its logging from the button menu.
4. Keep both antennas in a consistent orientation and height. Keep the walking radio in a consistent position relative to your body.
5. Record open line-of-sight paths first. These establish the radio, antenna, and local-noise baseline.
6. Walk paths whose direct lines cross the neighborhood's buildings at several distances and angles. Repeat routes in both directions at least three times.
7. Stop logging on both devices before transport or extraction.

The walk is a sample used to establish a global Building default; it does not create a neighborhood-only correction layer. Wall materials, windows, foliage, antenna placement, vehicles, multipath, weather, and local noise still vary, so repeat the survey in other construction types when possible.

## Apply the survey to the RF model

Load the survey and import its matching building footprints, then select **Calibrate buildings** in the Survey Export & Viewer. The fit uses valid received RSSI and failed probes, keeps the scenario path-loss exponent and the 0.3 dB/100 m inside-building term unchanged, and selects one fixed attenuation per crossed building using the same −4 dB delivery threshold as coverage and simulated packets. Calibration requires at least 20 received probes, 10 failed probes, five clear paths, and five building-obstructed paths.

After confirmation, the fitted value is applied to every Building in the current scenario—not only footprints near survey points. Buildings become `ATTENUATE` with a zero range cap; a building never creates an arbitrary hard stop after 0.3 mile. Loaded survey points remain a comparison overlay and do not locally repaint predicted coverage. Save the scenario after applying the result.

## Export and view both radios in MeshLab RF

After the walk, connect both survey nodes by USB, open MeshLab RF, and select **Survey logs** on the toolbar (or **File > Survey node export & viewer**). The viewer is hardware-model neutral: any future node model implementing the same MeshLab survey protocol can be selected and exported.

1. Select **Refresh ports** so every current serial port appears in both lists.
2. Choose the connected node under **Mobile port** or **Base port**. Selection immediately identifies the node, downloads its complete retained log into memory, validates the dump CRC and every record, and plots every available measurement. No save dialog is required.
3. Disconnect that node whenever loading completes, connect the other node, refresh the ports, and select it under its role. The first capture remains in memory; the second capture is merged with it and the table/map are regenerated without losing the first node. Both nodes never need to be connected simultaneously.
   A base-only load still plots every received probe using the mobile GPS embedded in that packet and the base's measured forward RSSI/SNR. Return reception is shown as **Not observed** until the mobile log is loaded; it is not incorrectly counted as a lost reply.
4. **Reload selected** is only needed when the log on a selected node changed after it was loaded.
5. **Save captured logs** is optional. Use it only when you want permanent binary dumps, decoded device CSVs, metadata, the combined device log, and `measurements.csv` outside the app.
6. Green, amber, and red map points show progressively weaker outward RSSI; a red X is a forward packet loss. Select a table row or map point to inspect that exchange and its return-link RSSI. Use **Fit all points on map** to frame the route.

To review an earlier export, select **Open saved export** and choose its `measurements.csv`. Clearing map points never deletes the exported files or the logs retained on either survey node.

The command-line extractor remains available as a fallback. It accepts either one node for recovery/later pairing or one mobile plus one base. From the repository root run:

```powershell
python tools\extract_survey.py
```

If automatic USB detection misses a tester, specify one or both ports explicitly:

```powershell
python tools\extract_survey.py --ports COM7
python tools\extract_survey.py --ports COM7 COM8
```

To extract the nodes at different times from the command line, pass the same explicit `--output-dir` for each one; the second run merges the earlier device CSV in that directory. The GUI retains the first capture in memory automatically and is simpler for sequential loading.

The extractor supports compact version 2 logs and remains compatible with earlier 128-byte version 1 dump files. It verifies that one board is mobile and one is base, downloads each retained log with an end-to-end CRC, validates every individual record, and writes a timestamped folder under `survey-data`. Keep the entire folder. It contains:

- an untouched binary dump, decoded CSV, and radio-profile JSON from each radio;
- `combined-device-log.csv`, containing every valid device event;
- `measurements.csv`, containing one joined calibration record per probe.

`measurements.csv` explicitly separates `forward_received` from `reply_received` and contains GPS lock/position, HDOP, satellites, RSSI, SNR, radio parameters, and packet-loss outcomes for the two directions.
