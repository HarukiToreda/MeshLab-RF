# Changelog

## Unreleased

### Fixed

- **Coordinate projection understated real-world distance away from the equator.** World coordinates (used for node/obstacle placement, path loss, Fresnel/terrain clearance, coverage and beacon radii, and map tile alignment) were raw Web Mercator meters, which inflate ground distance by `1 / cos(latitude)`. At this project's ~40.9°N test latitude that meant every simulated link used a distance about 32% longer than the real one, adding roughly 2.4 dB of phantom path loss to every link (worse at higher latitudes, none at the equator). Fixed at the source in `geography.latlon_to_world` / `world_to_latlon` / `world_viewport_to_mercator_bounds`, plus the handful of call sites in `ui.py` and `geography.build_terrain_grid` that independently re-derived the same Mercator math (map tile placement and sizing, cached-DEM elevation lookups, and initial view sizing from search/live-node reframing). Also corrected obstacle-import coverage area, which was quietly importing less real ground than the documented 12 km² per tile for the same reason.
- Saved scenario files predating this fix are migrated automatically on load (`coordinate_space: "CENTERED_MERCATOR"` → `"CENTERED_MERCATOR_TRUE_SCALE"`); node, obstacle, and terrain-grid coordinates are rescaled once, in place, so existing scenarios don't shift on the map.
