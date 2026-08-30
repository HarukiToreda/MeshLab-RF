# Changelog

## Unreleased

### Fixed

- Signal testers now wake directly instead of entering OTA mode.
- Battery gauges now refresh every five seconds.
- Paused metrics now refresh without repainting the full screen.

### Changed

- Survey collection now uses a scrolling nine-ping status log.
- Paused testers now show a metrics dashboard and require a hold to open the menu.
- A tap while collecting now switches between the live log and control center.
- Storage metrics now live only in the control center, not the menu.

## 1.0.1 - 2026-08-30

### Added

- Apple Silicon and Intel macOS builds.
- Incognito mode for hiding coordinates and map labels.
- Locally generated vector maps with smoother zoom and pan.
- Survey playback with animated RF pings.

### Fixed

- Faster beacon, packet, and live-mesh calculations.
- Complete dense obstacle imports without patchwork gaps.
- Correct geographic distances and RF path loss.
- Reliable window closing and clean-session file opening.
- Clearer survey labels and markers.
- Windows builds now stop on packaging errors.

### Changed

- Serial-port pickers now rescan automatically and hide Bluetooth clutter.
- Survey logs now uses one combined port picker.
- Closing never prompts; save explicitly with File → Save.
