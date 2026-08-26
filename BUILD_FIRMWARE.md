# Build and modify the signal tester firmware

The complete editable firmware project is in [`signal_tester/`](signal_tester/). The UF2 files in `artifacts/` are compiled outputs; they are not the source. It currently builds for the Heltec Mesh Node T114; other boards will get their own board/variant files and PlatformIO environments alongside it as they're added.

## Source files

- `signal_tester/src/main.cpp`: GPS, LoRa send/reply behavior, display, flash logging, and USB commands.
- `signal_tester/include/survey_config.h`: frequency, bandwidth, spreading factor, coding rate, transmit power, send interval, reply timeout, storage size, and T114 pin assignments.
- `signal_tester/include/survey_protocol.h`: over-the-air packet and stored-record formats plus CRC handling.
- `signal_tester/platformio.ini`: mobile/base build targets, board platform, libraries, and compiler settings.
- `signal_tester/boards/heltec_mesh_node_t114_signal.json`: nRF52840 board definition and memory layout.
- `signal_tester/linker/nrf52840_s140_v6_survey.ld`: protects the 640 KB internal-flash log partition from application code.
- `signal_tester/variants/heltec_mesh_node_t114/`: Arduino pin variant used by both builds.
- `signal_tester/scripts/make_uf2.py`: converts each compiled HEX image to a flashable UF2.
- `mesh_simulator/survey.py`: decodes compact and legacy records and joins the two link directions.
- `mesh_simulator/survey_device.py`: discovers selected serial ports, downloads/validates logs, and writes exports.
- `mesh_simulator/survey_calibration.py`: fits and globally applies per-building attenuation from a loaded survey.

Change the radio profile and timing in `include/survey_config.h`. Change device behavior, display text, log handling, or USB commands in `src/main.cpp`. Both radios must use matching radio and packet settings.

## Install the build tool

Install Python 3, then run:

```powershell
python -m pip install platformio
```

PlatformIO downloads the pinned framework and libraries declared in `platformio.ini` during the first build. The downloaded `.pio/` directory is a disposable build cache and is not source code.

## Build both firmware roles

From the repository root:

```powershell
cd signal_tester
python -m platformio run -e t114-survey-mobile -e t114-survey-base
```

Successful builds produce:

```text
signal_tester\.pio\build\t114-survey-mobile\heltec-t114-signal-mobile.uf2
signal_tester\.pio\build\t114-survey-base\heltec-t114-signal-base.uf2
```

The mobile image goes on the walking/GPS radio. The base image goes on the fixed receiver. Put each T114 into its UF2 bootloader, connect it by USB, and copy the corresponding UF2 onto the bootloader drive.

The build does not automatically copy outputs into `artifacts`. Checked-in ready-to-flash copies are kept there for convenience. To refresh them after changing the source, copy the two generated files to:

```text
artifacts\signal-tester\heltec-t114-signal-mobile.uf2
artifacts\signal-tester\heltec-t114-signal-base.uf2
```

## Rebuild after a change

Save the source change and run the same build command. PlatformIO recompiles only what changed. To force a completely clean rebuild:

```powershell
python -m platformio run --target clean
python -m platformio run -e t114-survey-mobile -e t114-survey-base
```

Field operation and two-radio data extraction are documented in [`docs/FIELD_SURVEY.md`](docs/FIELD_SURVEY.md).

## Verify the desktop integration

From the repository root, run:

```powershell
python -m unittest discover -s tests -v
.\build.ps1
```

The desktop build produces only `dist\MeshLabRF.exe`. The survey firmware UF2 files remain separate artifacts and are never bundled into another executable.
