# Changelog

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
