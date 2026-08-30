# Changelog

## Unreleased

### Added

- **Incognito mode.** View menu toggle that hides all coordinates (status bar, scale bar, node/obstacle fields, Survey GPS column, Horizon/Profile labels) and every street/place name on the map — for recording without revealing your location.
- **Locally-generated base map.** Map tiles (land use, water, boundaries, roads) now render from vector data instead of fetched raster images, so street/place labels are a separate layer Incognito can hide independently. Faster repeat loading, more parallel tile workers, and an instant zoom/pan placeholder so the map never looks blank while new tiles load.
- **Survey playback.** Walk a loaded survey over time with Play/Pause, a speed selector, and a scrub bar; the current position and RSSI highlight live on the map.
- **Animated ping during playback.** Each revealed probe shows as a pulse traveling to the base and back.

### Fixed

- **Beacon speed.** Dense maps now use the intended adaptive ray count, making coverage appear much sooner.
- **Packet and live-mesh speed.** Dense packet contours use adaptive sampling, and impossible obstacle intersections are filtered earlier.
- **Dense obstacle imports.** Fixed patchwork gaps and missing buildings. Capped cells now subdivide reliably, with downloaded results kept up to 20,000 buildings per tile.
- **Coordinate projection inflated link distance away from the equator** (up to ~32% longer, ~2.4 dB phantom path loss at this project's test latitude). Fixed at the source; old scenario files migrate automatically on load.
- **Survey map labels/markers were unreadable against the light map background.** Now use the app's dark-halo text styling.

### Changed

- Serial port pickers (Survey logs, COM Radio) now rescan automatically when opened — no manual refresh.
- Survey logs uses one combined port picker instead of separate mobile/base pickers.
- Bluetooth virtual COM ports no longer clutter the port pickers.
- Closing MeshLab RF no longer prompts to save — save explicitly with File → Save.
