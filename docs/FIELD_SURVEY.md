# T114 building-loss field survey

This survey uses two Heltec Mesh Node T114 radios. The **mobile** radio sends a zero-hop probe after each new GPS fix, no faster than once every five seconds. The fixed **base** radio records the probe's RSSI and SNR and immediately broadcasts a reply. The mobile radio records the reply's RSSI and SNR.

Both radios append their records to `/static/meshlab-survey.csv`. The log is not cleared on reboot. A random session ID separates each power-on session.

## What the display shows

- Mobile: each probe sent, each reply received with the outward and return RSSI/SNR, and each reply timeout.
- Base: each probe received with RSSI/SNR and confirmation that its reply was sent.

The separate logs matter. A mobile timeout alone cannot tell whether the probe failed on the outward path or the reply failed on the return path. The base log resolves that ambiguity and also preserves complete packet-loss observations.

## Before the walk

1. Configure the two radios with the same primary channel, channel key, modem preset, frequency slot, and legal LoRa region. The region must not be `UNSET`.
2. Flash `heltec-t114-survey-mobile.uf2` on the walking radio and `heltec-t114-survey-base.uf2` on the fixed radio using the normal T114 UF2 bootloader process. Do not perform a full erase, because that would remove existing configuration and stored survey data.
3. Put the base at a fixed, accurately known location with a clear view of the sky. Wait for both radios to obtain GPS locks.
4. Use comparable antennas and keep antenna orientation, radio height, and body placement consistent. Record any antenna or installation differences separately.

The firmware records GPS validity, coordinates, altitude, satellite count, PDOP, radio region/preset/frequency/power, channel utilization, packet IDs, and bidirectional RSSI/SNR. Rows with an invalid base GPS position remain useful for RF diagnostics but should not be used for geographic calibration.

## Collect useful calibration data

Include open line-of-sight paths as a reference, then paths whose direct lines cross the neighborhood's buildings. Walk multiple directions and distances, and repeat routes at least three times. Consistent mounting is important because body shadowing can otherwise look like building loss.

This produces a local effective building-loss calibration, not one universal physical value. Wall material, windows, foliage, antenna placement, moving vehicles, multipath, weather, and local noise all affect the result. The open-path samples let us estimate the radio/antenna baseline before assigning the residual loss to buildings.

## Extract both radios

After the walk, connect **both T114s by USB at the same time**. From the project root run:

```powershell
python tools\extract_survey.py
```

If automatic port detection does not find exactly two T114s, name both ports explicitly:

```powershell
python tools\extract_survey.py --ports COM7 COM8
```

The extractor opens the ports one at a time, downloads both device logs, and writes a timestamped folder under `survey-data`. Keep the whole folder. It contains:

- one untouched CSV from each radio;
- `combined-device-log.csv`, containing every device event;
- `measurements.csv`, containing one joined calibration record per probe and base.

`measurements.csv` explicitly distinguishes `forward_received` from `reply_received`. Missing RSSI is retained as packet-loss evidence instead of being discarded.

Once the two logs are available, the calibration step will compare measured link loss with MeshLab RF's prediction, fit the clear-path bias first, and then estimate robust local excess loss for the building intersections. The simulator's building values should only be changed after that comparison.
