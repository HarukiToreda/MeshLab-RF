# Map, terrain, and obstacle data

MeshLab RF uses public geographic services for its map, elevation, search, and imported obstructions.

## Sources

| Data | Source |
|---|---|
| Street map | OpenStreetMap raster tiles |
| Topographic map | OpenTopoMap raster tiles |
| Search | OpenStreetMap Nominatim |
| Elevation | Mapzen Terrarium tiles hosted on AWS |
| Buildings | Overture Maps building theme |
| Forests and building fallback | OpenStreetMap ways through Overpass |

Map and elevation tiles load for the visible area. Terrain also refreshes after startup, searches, live-node reframing, and moving nodes outside current coverage. Obstacle data loads only when **Import obstacles** is selected.

## Coordinate system

Node and obstacle positions are stored as `x`/`y` in true ground meters, center-relative to the scenario's map location. Latitude/longitude is projected with Web Mercator and then rescaled by `cos(map center latitude)`, since raw Web Mercator inflates ground distance by `1 / cos(latitude)` away from the equator; without that correction, one coordinate unit would represent more real distance at higher latitudes, quietly adding excess path loss to every simulated link. Scenario files saved before this correction (`coordinate_space: "CENTERED_MERCATOR"`) are rescaled automatically the first time they're opened.

## Display and terrain

Map tiles are converted to high-contrast grayscale. **Terrain only** replaces the map with hillshade, contours, and elevation labels generated from elevation data.

The saved terrain grid covers the visible area and scene objects. It uses 49–129 columns, 25–97 rows, and no more than 36 source DEM tiles. Terrarium RGB values are decoded to meters and stored in the scenario.

Nodes use the most detailed cached elevation available, with the saved grid as fallback. Disabling terrain in RF paths does not erase node ground elevations.

## Obstacle import

Obstacle imports use these limits:

- Each tile covers at most 12 km² (4.63 mi²).
- The viewport uses up to a 3×3 tile grid. Extremely wide views import their central region.
- Each tile starts with a 12,000-building budget.
- Capped Overture cells subdivide for up to two more levels.
- Downloaded results are kept up to 20,000 buildings per tile.
- Results at the hard cap are distributed across completed cells.

Saturation is checked before polygon conversion. Invalid geometry cannot hide a capped query.

OSM provides the building fallback and up to 500 forest ways. Duplicate provider IDs are skipped. Re-importing adds missing footprints without duplicating existing buildings.

Building height uses the supplied height, then floors × 3 m, then a 6 m default (an unverified footprint with no source height/floor data reads as roughly 2 storeys rather than 4, since a single overestimated import building was enough to falsely mark an otherwise-clear long link as blocked). Forests default to 18 m. RF loss values never come from the map provider. MeshLab's global field-survey Building default is 10.8 dB per crossed footprint plus 0.3 dB/100 m inside it, with attenuation rather than a distance cutoff; imported buildings receive the same default as manually drawn buildings.

Survey measurements plotted on the map are a comparison layer. They do not modify nearby map data, building footprints, terrain, or predicted coverage. Applying **Calibrate buildings** changes the Building value across the entire current scenario, including unsurveyed footprints.

## Cache and privacy

Data is cached under:

`%LOCALAPPDATA%\MeshLabRF\map-cache`

- Map and elevation tiles have no automatic expiry.
- Overture extracts expire after seven days.
- Obstacle cache entries retain query-saturation status.
- The first re-import after this update refreshes legacy obstacle cache entries.
- Terrain saved in a scenario is separate from this cache.

Search text and visible/requested geographic bounds are sent to the relevant public services. Providers may be incomplete, rate-limited, unavailable, or changed independently of MeshLab RF.

The canvas displays attribution for OpenStreetMap, OpenTopoMap, Overture Maps, and Mapzen/AWS as applicable.

Map footprints and global elevation are planning data, not survey-grade ground truth. Verify important geometry and heights in the field.
