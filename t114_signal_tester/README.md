# Heltec T114 standalone signal tester

This is a small, self-contained PlatformIO firmware project for a paired RF/GPS field survey. It uses only the T114's nRF52840, SX1262, L76K GPS, ST7789 display, external QSPI flash, and USB serial port.

Build both roles:

```powershell
python -m platformio run
```

Build one role:

```powershell
python -m platformio run -e t114-survey-mobile
python -m platformio run -e t114-survey-base
```

The generated UF2 files are named `heltec-t114-signal-mobile.uf2` and `heltec-t114-signal-base.uf2` inside their corresponding `.pio/build` directories.

The default radio profile is US915 at 906.875 MHz. Review `include/survey_config.h` before use in another regulatory region. Field and extraction instructions are in [`docs/FIELD_SURVEY.md`](../docs/FIELD_SURVEY.md).
