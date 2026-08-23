# Build and modify the Heltec T114 signal tester

The complete editable firmware project is in [`t114_signal_tester/`](t114_signal_tester/). The UF2 files in `artifacts/` are compiled outputs; they are not the source.

## Source files

- `t114_signal_tester/src/main.cpp`: GPS, LoRa send/reply behavior, display, flash logging, and USB commands.
- `t114_signal_tester/include/survey_config.h`: frequency, bandwidth, spreading factor, coding rate, transmit power, send interval, reply timeout, storage size, and T114 pin assignments.
- `t114_signal_tester/include/survey_protocol.h`: over-the-air packet and stored-record formats plus CRC handling.
- `t114_signal_tester/platformio.ini`: mobile/base build targets, board platform, libraries, and compiler settings.
- `t114_signal_tester/boards/heltec_mesh_node_t114_signal.json`: nRF52840 board definition and memory layout.
- `t114_signal_tester/variants/heltec_mesh_node_t114/`: Arduino pin variant used by both builds.
- `t114_signal_tester/scripts/make_uf2.py`: converts each compiled HEX image to a flashable UF2.

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
cd C:\Mesh-Simulator\t114_signal_tester
python -m platformio run -e t114-survey-mobile -e t114-survey-base
```

Successful builds produce:

```text
t114_signal_tester\.pio\build\t114-survey-mobile\heltec-t114-signal-mobile.uf2
t114_signal_tester\.pio\build\t114-survey-base\heltec-t114-signal-base.uf2
```

The mobile image goes on the walking/GPS radio. The base image goes on the fixed receiver. Put each T114 into its UF2 bootloader, connect it by USB, and copy the corresponding UF2 onto the bootloader drive.

## Rebuild after a change

Save the source change and run the same build command. PlatformIO recompiles only what changed. To force a completely clean rebuild:

```powershell
python -m platformio run --target clean
python -m platformio run -e t114-survey-mobile -e t114-survey-base
```

Field operation and two-radio data extraction are documented in [`docs/FIELD_SURVEY.md`](docs/FIELD_SURVEY.md).
