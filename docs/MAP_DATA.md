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

## Display and terrain

Map tiles are converted to high-contrast grayscale. **Terrain only** replaces the map with hillshade, contours, and elevation labels generated from elevation data.

The saved terrain grid covers the visible area and scene objects. It uses 49–129 columns, 25–97 rows, and no more than 36 source DEM tiles. Terrarium RGB values are decoded to meters and stored in the scenario.

Nodes use the most detailed cached elevation available, with the saved grid as fallback. Disabling terrain in RF paths does not erase node ground elevations.

## Obstacle import

Each import covers the complete visible viewport but is limited to 12 km² (4.63 mi²). Import adjacent views to cover a larger area.

The viewport is divided into 4–16 geographic cells. Overture cells that reach their 750-building query limit are divided into four and retried for two more levels. Results are distributed across cells, with a total building limit between 6,000 and 20,000 depending on the initial grid.

OSM forest ways use `landuse=forest` or `natural=wood`; up to 500 are added per import. OSM buildings are used if Overture fails. Duplicate provider IDs are skipped.

Building height uses the supplied height, then floors × 3 m, then a 12 m default. Forests default to 18 m. RF loss values never come from the map provider. MeshLab's global field-survey Building default is 10.8 dB per crossed footprint plus 0.3 dB/100 m inside it, with attenuation rather than a distance cutoff; imported buildings receive the same default as manually drawn buildings.

Survey measurements plotted on the map are a comparison layer. They do not modify nearby map data, building footprints, terrain, or predicted coverage. Applying **Calibrate buildings** changes the Building value across the entire current scenario, including unsurveyed footprints.

## Cache and privacy

Data is cached under:

`%LOCALAPPDATA%\MeshLabRF\map-cache`

Map and elevation tiles currently have no automatic expiry. Overture extracts expire after seven days. Terrain saved in a scenario is separate from this cache.

Search text and visible/requested geographic bounds are sent to the relevant public services. Providers may be incomplete, rate-limited, unavailable, or changed independently of MeshLab RF.

The canvas displays attribution for OpenStreetMap, OpenTopoMap, Overture Maps, and Mapzen/AWS as applicable.

Map footprints and global elevation are planning data, not survey-grade ground truth. Verify important geometry and heights in the field.
