# Changelog

## Unreleased

### Added

- **Survey playback.** The Survey logs viewer can now walk a loaded survey on the map over time instead of showing every point at once. Play/Pause, a speed selector (1x–600x), and a scrub bar sit above the measurement table; the current position is highlighted on the map with its live RSSI, and points reveal in GPS-timestamp order as playback advances.
- **Animated ping during playback.** As each probe is revealed, a short animated pulse travels from the mobile position out to the base, then back again if the base replied — a lost probe visibly fades out partway instead of completing the trip.

### Fixed

- **Coordinate projection understated real-world distance away from the equator.** World coordinates (used for node/obstacle placement, path loss, Fresnel/terrain clearance, coverage and beacon radii, and map tile alignment) were raw Web Mercator meters, which inflate ground distance by `1 / cos(latitude)`. At this project's ~40.9°N test latitude that meant every simulated link used a distance about 32% longer than the real one, adding roughly 2.4 dB of phantom path loss to every link (worse at higher latitudes, none at the equator). Fixed at the source in `geography.latlon_to_world` / `world_to_latlon` / `world_viewport_to_mercator_bounds`, plus the handful of call sites in `ui.py` and `geography.build_terrain_grid` that independently re-derived the same Mercator math (map tile placement and sizing, cached-DEM elevation lookups, and initial view sizing from search/live-node reframing). Also corrected obstacle-import coverage area, which was quietly importing less real ground than the documented 12 km² per tile for the same reason.
- Saved scenario files predating this fix are migrated automatically on load (`coordinate_space: "CENTERED_MERCATOR"` → `"CENTERED_MERCATOR_TRUE_SCALE"`); node, obstacle, and terrain-grid coordinates are rescaled once, in place, so existing scenarios don't shift on the map.

### Changed

- **Serial port pickers no longer need a manual refresh.** Both the Survey logs and COM Radio port dropdowns now rescan available ports the moment you open them, so the separate "Refresh ports" / "Refresh" buttons are gone.
- **Survey logs now uses one port picker instead of two.** The mobile/base survey nodes are never plugged in at the same time in practice, so "Mobile port" and "Base port" are combined into a single "Survey node port" dropdown. The connected device's own reported role determines where its log goes; captures for different roles still merge together exactly as before, whichever order you connect them in.
- **Bluetooth serial ports are excluded from both port pickers.** Windows' virtual "Standard Serial over Bluetooth link" COM ports never carry a real radio and only added clutter to the list.
- **Closing MeshLab RF no longer prompts to save the scenario.** Save explicitly with **File → Save** when you want to keep changes; closing the window just closes it.

### Fixed (UI)

- **Survey map labels and markers were unreadable against the light map background.** "SURVEY BASE," per-point RSSI labels, and the selected/current point outlines were plain white and blended into the map's light streets/terrain. Labels now use the app's existing dark-text/white-halo styling, and marker/ping outlines get a dark backing so they stay visible on the light theme.
