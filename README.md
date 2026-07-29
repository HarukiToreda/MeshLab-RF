# MeshLab RF

MeshLab RF is an unreleased Windows desktop tool for planning Meshtastic networks. It lets you place radios on a real map, import buildings and forests, use terrain elevation, and watch packets travel from node to node.

Use it to explore coverage, find likely dead spots, compare node locations and heights, and understand how hop limits and node roles affect a mesh. It is a planning aid, not a replacement for an RF site survey.

## Run it

Open [MeshLabRF.exe](dist/MeshLabRF.exe).

The app starts with a map but no nodes. Internet access is needed for new map tiles, search, terrain, and obstacle data. Saved scenarios and cached data can be used offline.

## Simple guide

1. Search for the location you want to study.
2. Select **Node**, then click the map to place as many nodes as needed. The tool stays selected.
3. To add many nodes, choose **Random nodes**. They will be spread across the visible area.
4. Zoom in and choose **Import obstacles** to load buildings and forests. Imports are limited to 12 km² (4.63 mi²) at a time.
5. Select a node and open **Properties** to set its role, radio, power, channel, and height.
6. Open **Packet**, choose the source and destination, set the hop limit, and select **Send packet**.
7. Open **Results** for delivery details, RF values, drops, and collisions.

Use the mouse wheel to zoom, right-drag to pan, and **Fit** to frame the current nodes. **Panels** opens or hides the side tabs.

## Reading the simulation

- Broadcasts radiate from every node that receives and rebroadcasts the packet.
- Hop colors and `H0`–`H7` badges show how far the packet traveled.
- Nodes not reached by the completed simulation turn gray.
- If the source reaches nobody, its coverage boundary remains visible to help place another node.
- Clicking a reached node shows its complete first-arrival path.
- Starting another simulation automatically clears the previous trace.
- **Clear hops** removes the trace without running another simulation.
- **Hop lines** can hide individual hop layers.

Direct messages first use flooding when no route is known. If an ACK confirms the route, later DMs use only directed hop lines. A failed learned route is removed and the simulator falls back to flooding.

## Terrain, height, and obstacles

Terrain affects line of sight and Fresnel clearance. Each node has:

- **Terrain elevation MSL**: ground height above mean sea level.
- **Installation height AGL**: antenna height above the ground.

Nodes automatically follow terrain unless their ground elevation is manually overridden. A rooftop antenna should use the building height plus any mast height as its AGL.

Imported buildings and forests are editable RF obstructions. Building and vegetation loss values are planning defaults and should be adjusted when better local information is available.

The **Terrain only** option shows elevation contours without roads and labels. **Show map tiles** changes only the background and does not change simulation results.

## Live radio

Open **COM Radio**, choose the Meshtastic radio’s Windows COM port, and connect. MeshLab RF reads the radio’s known NodeDB and plots nodes with valid positions.

Updates are merged by node number, so reconnecting does not duplicate nodes. Nodes without coordinates remain listed but are not placed. Valid MSL altitude is used when available; impossible altitude is ignored while the position is kept and placed above local terrain.

The connection is read-only. It does not transmit packets or change radio settings.

## Accuracy

Results depend on terrain and map quality, actual radio power, antennas, cable loss, local noise, building materials, foliage, and the selected path-loss settings. Verify important placements with real measurements.

Technical details:

- [Firmware and RF model](docs/FIRMWARE_MODEL.md)
- [Map, terrain, and obstacle data](docs/MAP_DATA.md)

## Development

```powershell
python main.py
python -m unittest discover -s tests -v
.\build.ps1
```

The build output is `dist\MeshLabRF.exe`.
