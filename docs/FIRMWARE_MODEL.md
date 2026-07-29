# Firmware and RF model

MeshLab RF is based on the Meshtastic firmware checkout at `C:\firmware`. It models behavior that affects visible packet reach and timing; it is not a full firmware emulator.

## Firmware behavior

The implementation follows these local firmware areas:

- roles and rebroadcast modes from `protobufs/meshtastic/config.proto`;
- modem presets from `src/mesh/MeshRadio.h`;
- hop fields, airtime, relay timing, and contention from `src/mesh/MeshTypes.h`, `RadioInterface.h`, and `RadioInterface.cpp`;
- duplicate handling and flooding from `PacketHistory.cpp` and `FloodingRouter.cpp`;
- channel, next-hop, and reliable routing behavior from `Router.cpp`, `NextHopRouter.cpp`, and `ReliableRouter.cpp`.

Broadcasts and unknown direct routes use managed flooding. The simulator applies hop limits, role-based relay delay, duplicate cancellation, rebroadcast-mode filtering, radio compatibility, channel decoding, opaque relays, airtime, collisions, and capture.

A DM with **Request ACK** can learn its first-arrival path when the reverse links also work. Later DMs use that stored path. If a node, channel, relay rule, RF link, or ACK path fails, the route is removed and the same run falls back to flooding.

MeshLab RF stores one complete path per source/destination pair. It does not reproduce every node’s firmware next-hop table or full retry state machine.

## RF calculation

Three-dimensional path loss is:

`PL(d) = FSPL(1 m, f) + 10 n log10(d)`

Received power includes conducted TX power, both antenna gains, both cable losses, weather, obstructions, terrain, and optional seeded shadowing.

Receiver noise is:

`N = −174 + 10 log10(BW Hz) + noise figure`

Required SNR ranges from about `−2.5 dB` at SF5 to `−20 dB` at SF12. Link margin becomes a reception probability when stochastic mode is enabled.

Radios must have compatible frequency, bandwidth, spreading factor, and coding rate. Channel/PSK behavior is represented by matching channel-name strings; encryption is not simulated.

Hardware profiles provide editable planning values for common 13, 20, 22, and 29–30 dBm radios. They do not determine regional legality or replace measured output.

## Terrain and obstructions

Normal antenna elevation is terrain MSL plus installation AGL. A valid live MSL altitude may be used instead.

Terrain is sampled along each path:

- terrain at or above the antenna-to-antenna line blocks reception;
- terrain inside 60% of the first Fresnel zone adds up to 24 dB loss.

Obstructions add fixed and distance-through-material loss. Their modes are:

- `ATTENUATE`: subtract loss;
- `BLOCK`: stop the path;
- `LIMIT_AFTER`: stop reception past a set distance beyond the obstruction.

An RF path that clears an obstruction and 60% of its first Fresnel zone is unaffected by it.

## Important limits

MeshLab RF does not model antenna patterns, polarization, reflections, building interiors, spatially correlated fading, automatic background traffic, duty-cycle enforcement, sleep schedules, encryption cost, MQTT, Bluetooth, or every firmware retry.

Coverage contours use a representative compatible receiver. Exact delivery still uses each real target node’s settings. The favorite-router hop-preservation exception is not simulated.

The model should be calibrated with field measurements before making important deployment decisions.
