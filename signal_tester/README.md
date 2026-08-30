# Heltec T114 standalone signal tester

This is a small, self-contained PlatformIO firmware project for a paired RF/GPS field survey. It uses only the T114's nRF52840, SX1262, L76K GPS, ST7789 display, user button, and USB serial port. Logs use a linker-reserved region of the nRF52840's internal flash; no external flash chip is required.

Build both roles:

```powershell
python -m platformio run
```

Build one role:

```powershell
python -m platformio run -e t114-survey-mobile
python -m platformio run -e t114-survey-base
```

The generated UF2 files are named `heltec-t114-signal-mobile.uf2` and `heltec-t114-signal-base.uf2` inside their corresponding `.pio/build` directories. Checked-in ready-to-flash copies are in [`artifacts/signal-tester`](../artifacts/signal-tester); those binaries are outputs, while this directory contains the editable source.

Logging is paused at boot. Hold the T114 button for 500 ms to open the menu; a tap does nothing while paused. Tap to move and hold to select inside the menu. Power-off waits for release, and the next press wakes directly into the tester. Logs remain until `WIPE ALL LOGS` is confirmed.

The battery gauge refreshes every five seconds, including while logging is paused.

The paused control center shows GPS quality, coordinates, speed, storage, sound, radio settings, device ID, and battery.

While collecting, both testers show a nine-row live log. New pings scroll in at the bottom with quality, RSSI/SNR, and reply status; the mobile updates `WAIT` to `YES` or `NO` when each result arrives.

While collecting, tap to switch between the live log and control center. Hold from either view to open the menu.

The mobile sends its first probe only after consecutive fresh fixes have at least six satellites and HDOP no worse than 2.00. Walking mode requires five good fixes. At 20 km/h or faster, vehicle mode requires seven good fixes and judges position changes against the GPS-reported speed instead of rejecting normal driving motion; speeds above 180 km/h remain rejected as implausible for this survey. After the first sample, another probe requires at least five meters of movement when walking or 15 meters while driving, as well as the five-second rate limit, so a stationary tester does not fill the log with duplicate positions. These thresholds are configurable in `include/survey_config.h`.

The compact version 2 format stores 8,140 total records in a 640 KB internal-flash partition. A session uses one boot record and then one record per completed exchange, so one uninterrupted session holds up to 8,139 exchanges (roughly 11.3 hours at the default five-second interval). The control center shows used and free records and KB. The updated USB extractor also understands legacy version 1 dump files.

USB serial at 115200 baud exposes `MESHLAB_INFO`, `MESHLAB_DUMP`, and the deliberately explicit `MESHLAB_CLEAR YES` command for model-neutral desktop or command-line extraction. A dump includes role, node ID, record format, stored count, radio profile, raw records, and an end-to-end CRC. The desktop viewer lets the user select any serial port; it does not require both radios to be connected at once.

The default radio profile is US915 at 906.875 MHz. Review `include/survey_config.h` before use in another regulatory region. Field and extraction instructions are in [`docs/FIELD_SURVEY.md`](../docs/FIELD_SURVEY.md).
