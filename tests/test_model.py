import io
import math
import unittest

import numpy as np
from PIL import Image, ImageColor
from shapely.geometry import MultiPolygon, Polygon

from mesh_simulator.model import (
    BeaconProfile,
    BeaconRadialSample,
    BeaconRay,
    Environment,
    HARDWARE_POWER_PROFILE_KEYS,
    MIN_DECODE_MARGIN_DB,
    Node,
    Obstacle,
    PacketConfig,
    PRESETS,
    REGION_BANDS,
    PropagationModel,
    Scenario,
    SimulationEngine,
    SimulationResult,
    create_demo_scenario,
    dbm_to_watts,
    dm_route_key,
    hardware_power_profile,
    meshtastic_default_frequency_mhz,
    preset_parameters,
)
from mesh_simulator.geography import (
    MapDataService,
    OBSTACLE_IMPORT_MAX_AREA_M2,
    grayscale_map_tile,
    latlon_to_mercator,
    latlon_to_world,
    mercator_to_tile,
    overture_rows_to_elements,
    obstacle_import_plan,
    split_geographic_bounds,
    world_to_latlon,
    world_viewport_to_mercator_bounds,
)
from mesh_simulator.live_radio import parse_live_node
from mesh_simulator.survey_calibration import BuildingCalibration, apply_building_calibration
from mesh_simulator.ui import (
    MAPLESS_BACKGROUND,
    MAX_CANVAS_ZOOM,
    MIN_CANVAS_ZOOM,
    MeshSimulatorApp,
    build_coverage_contours,
    build_terrain_visual,
    decode_terrarium_elevations,
    first_hop_coverage_to_retain,
    format_area_value,
    format_distance_value,
    layout_node_labels,
    packet_path_node_ids,
    result_uses_coverage_ripples,
    sample_elevation_array,
    spread_random_points,
    spread_random_points_in_regions,
    transmitter_ids_by_hop,
)


class ModelTests(unittest.TestCase):
    def test_hardware_power_profiles_recognize_current_amplified_devices(self):
        self.assertEqual(hardware_power_profile("RAK3401").recommended_dbm, 30)
        self.assertEqual(hardware_power_profile("TBEAM_1_WATT").recommended_dbm, 30)
        self.assertEqual(hardware_power_profile("STATION_G2").recommended_dbm, 30)
        self.assertEqual(hardware_power_profile("RAK4631").recommended_dbm, 22)
        self.assertAlmostEqual(dbm_to_watts(30), 1.0)
        self.assertAlmostEqual(dbm_to_watts(22), 0.158489, places=5)

    def test_hardware_power_profiles_include_high_power_and_one_watt_devices(self):
        labels = "\n".join(HARDWARE_POWER_PROFILE_KEYS)
        for model in (
            "WiFi LoRa 32 V4 HP",
            "Wireless Tracker V2",
            "Mesh Node T096",
            "MeshTower V2",
            "RAK WisMesh 1W",
            "T-Beam 1W",
            "Station G2",
            "MeshToad V3",
        ):
            self.assertIn(model, labels)

        cases = {
            "HELTEC_V4_R8": ("WiFi LoRa 32 V4 HP", 28.0, 29.0),
            "HELTEC_WIRELESS_TRACKER_V2": ("Wireless Tracker V2", 28.0, 29.0),
            "HELTEC_MESH_NODE_T096": ("Mesh Node T096", 28.0, 29.0),
            "HELTEC_MESHTOWER_V2": ("MeshTower V2", 30.0, 30.0),
            "RAK_WISMESH_STATION_HP": ("RAK WisMesh 1W", 30.0, 30.0),
            "RAK_WISMESH_REPEATER_MINI_HP": ("RAK WisMesh 1W", 30.0, 30.0),
            "NULLHOP_MESHTOAD_V3": ("MeshToad V3", 30.0, 30.0),
        }
        for hardware_name, (label, recommended, maximum) in cases.items():
            with self.subTest(hardware_name=hardware_name):
                profile = hardware_power_profile(hardware_name)
                self.assertIn(label, profile.key)
                self.assertEqual(profile.recommended_dbm, recommended)
                self.assertEqual(profile.maximum_dbm, maximum)

        # Existing scenario files using the former grouped Heltec label retain
        # the same PA-class behavior after migration to the named V4 profile.
        legacy = hardware_power_profile("Heltec PA models (29 dBm)")
        self.assertIn("WiFi LoRa 32 V4 HP", legacy.key)
        self.assertEqual(legacy.recommended_dbm, 28.0)

    def test_hardware_power_profiles_include_popular_device_families(self):
        labels = "\n".join(HARDWARE_POWER_PROFILE_KEYS)
        for model in ("T114", "RAK4631", "T-Beam", "T-Deck", "T-Echo", "T1000-E", "XIAO"):
            self.assertIn(model, labels)

        cases = {
            "HELTEC_MESH_NODE_T114": ("Heltec Mesh Node T114", 21.0, 22.0),
            "HELTEC_V3": ("Heltec LoRa 32 V3", 21.0, 22.0),
            "RAK4631": ("RAK WisBlock RAK4631", 22.0, 22.0),
            "TBEAM": ("LILYGO T-Beam /", 22.0, 22.0),
            "T_DECK_PRO": ("LILYGO T-Deck", 22.0, 22.0),
            "T_ECHO_PLUS": ("LILYGO T-Echo", 22.0, 22.0),
            "SENSECAP_CARD_TRACKER_T1000_E": ("Seeed SenseCAP T1000-E", 22.0, 22.0),
            "SEEED_XIAO_NRF52840_KIT": ("Seeed XIAO", 22.0, 22.0),
        }
        for hardware_name, (label, recommended, maximum) in cases.items():
            with self.subTest(hardware_name=hardware_name):
                profile = hardware_power_profile(hardware_name)
                self.assertIn(label, profile.key)
                self.assertEqual(profile.recommended_dbm, recommended)
                self.assertEqual(profile.maximum_dbm, maximum)

    def test_specific_and_amplified_names_beat_shorter_family_aliases(self):
        self.assertIn("T-Beam 1W", hardware_power_profile("LILYGO_TBEAM_1_WATT").key)
        self.assertEqual(hardware_power_profile("LILYGO_TBEAM_1_WATT").recommended_dbm, 30.0)
        self.assertIn("T114", hardware_power_profile("HELTEC_MESH_NODE_T114_SX1262").key)
        self.assertIn("Generic SX1262", hardware_power_profile("custom SX1262 module").key)

    def test_one_watt_source_has_eight_db_more_link_margin_than_sx1262(self):
        target = Node(name="Target", x=10_000, y=0)
        standard = Node(name="Standard", x=0, y=0, tx_power_dbm=22)
        amplified = Node(name="One watt", x=0, y=0, tx_power_dbm=30)
        standard_scenario = Scenario(nodes=[standard, target])
        amplified_scenario = Scenario(nodes=[amplified, target])
        standard_scenario.environment.stochastic = False
        amplified_scenario.environment.stochastic = False
        standard_link = PropagationModel(standard_scenario).link(standard, target)
        amplified_link = PropagationModel(amplified_scenario).link(amplified, target)
        self.assertAlmostEqual(amplified_link.margin_db - standard_link.margin_db, 8.0, places=6)
        self.assertGreater(
            PropagationModel(amplified_scenario).unobstructed_range_m(amplified, target),
            PropagationModel(standard_scenario).unobstructed_range_m(standard, target),
        )

    def test_zoom_limit_supports_continental_view(self):
        self.assertLessEqual(MIN_CANVAS_ZOOM, 0.001)

    def test_sidebar_tab_selection_stays_in_the_main_window(self):
        class Root:
            def __init__(self):
                self.after_idle_calls = 0

            def after_idle(self, callback):
                self.after_idle_calls += 1
                callback()

        class Tabs:
            def __init__(self):
                self.selected: list[int] = []

            def select(self, index):
                self.selected.append(index)

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.root = Root()
        app.sidebar_tabs = Tabs()
        renders: list[str] = []
        app.render_canvas = lambda: renders.append("render")

        app.show_sidebar_tab("Live Radio")

        self.assertEqual(app.sidebar_tabs.selected, [4])
        self.assertEqual(renders, ["render"])

    def test_property_change_reveals_sticky_apply_bar_until_applied(self):
        class ApplyBar:
            def __init__(self):
                self.shown = False

            def grid(self):
                self.shown = True

            def grid_remove(self):
                self.shown = False

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.object_form_dirty = False
        app.object_apply_bar = ApplyBar()
        app.get_selected = lambda: object()

        app._object_form_value_changed()

        self.assertTrue(app.object_form_dirty)
        self.assertTrue(app.object_apply_bar.shown)

        app._set_object_form_clean()

        self.assertFalse(app.object_form_dirty)
        self.assertFalse(app.object_apply_bar.shown)

    def test_zoom_can_continue_past_the_old_twenty_times_cap(self):
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.zoom = 20.0
        app.view_x = 0.0
        app.view_y = 0.0
        app.screen_to_world = lambda x, y: (x / app.zoom, y / app.zoom)
        app.render_canvas = lambda: None
        event = type("WheelEvent", (), {"x": 500, "y": 300, "delta": 120})()

        app._canvas_wheel(event)

        self.assertGreater(app.zoom, 20.0)
        self.assertLessEqual(app.zoom, MAX_CANVAS_ZOOM)

    def test_repeated_zoom_preview_resets_raster_anchor(self):
        class PreviewCanvas:
            def __init__(self):
                self.coordinates: list[tuple[int, int, int]] = []

            @staticmethod
            def find_withtag(_tag: str) -> tuple[int, ...]:
                return (17,)

            @staticmethod
            def itemconfigure(_item: int, **_values: object) -> None:
                return None

            def coords(self, item: int, x: int, y: int) -> None:
                self.coordinates.append((item, x, y))

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.canvas = PreviewCanvas()
        app.zoom_preview_active_tags = {"geographic"}
        app._transformed_zoom_source = lambda *_args, **_kwargs: object()
        app._paste_zoom_photo = lambda *_args, **_kwargs: (object(), False)

        app._zoom_preview_layer(
            object(),
            (1, 1, 0.0, 0.0, 1.0),
            tag="geographic",
            photo_attribute="zoom_geographic_photo",
        )

        self.assertEqual(app.canvas.coordinates, [(17, 0, 0)])

    def test_wheel_events_refresh_the_preview_immediately_every_tick(self):
        """A deferred-by-one-frame preview let the map raster lag one tick
        behind the vector items canvas.scale() moves instantly, which read as
        jitter/flicker on every wheel tick -- most visible with nodes and
        obstacles on screen since those are the vectors racing ahead of it.
        Every wheel event must repaint the preview synchronously, matching
        the beacon-active path, so nothing is ever left trailing behind."""

        class Root:
            def __init__(self) -> None:
                self.callbacks: list[tuple[int, object]] = []

            def after(self, delay: int, callback):
                token = f"after-{len(self.callbacks)}"
                self.callbacks.append((delay, callback))
                return token

            @staticmethod
            def after_cancel(_token: str) -> None:
                return None

        class Canvas:
            @staticmethod
            def scale(*_args: object) -> None:
                return None

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.root = Root()
        app.canvas = Canvas()
        app.zoom = 1.0
        app.view_x = 0.0
        app.view_y = 0.0
        app.zoom_preview_after = None
        app.zoom_render_after = None
        app.render_after = None
        app.beacon_profile = None
        app.screen_to_world = lambda x, y: (
            app.view_x + x / app.zoom,
            app.view_y + y / app.zoom,
        )
        preview_calls: list[str] = []
        app._render_zoom_preview = lambda: preview_calls.append("preview")
        event = type("WheelEvent", (), {"x": 500, "y": 300, "delta": 120})()

        app._canvas_wheel(event)
        app._canvas_wheel(event)

        self.assertEqual(preview_calls, ["preview", "preview"])
        self.assertFalse(any(delay == 16 for delay, _callback in app.root.callbacks))

    def test_active_beacon_zoom_refreshes_preview_without_anchor_delay(self):
        class Root:
            def __init__(self) -> None:
                self.callbacks: list[tuple[int, object]] = []

            def after(self, delay: int, callback):
                token = f"after-{len(self.callbacks)}"
                self.callbacks.append((delay, callback))
                return token

            @staticmethod
            def after_cancel(_token: str) -> None:
                return None

        class Canvas:
            @staticmethod
            def scale(*_args: object) -> None:
                return None

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.root = Root()
        app.canvas = Canvas()
        app.zoom = 1.0
        app.view_x = 0.0
        app.view_y = 0.0
        app.zoom_preview_after = None
        app.zoom_render_after = None
        app.render_after = None
        app.beacon_profile = object()
        app.screen_to_world = lambda x, y: (
            app.view_x + x / app.zoom,
            app.view_y + y / app.zoom,
        )
        preview_calls: list[str] = []
        app._render_zoom_preview = lambda: preview_calls.append("preview")
        event = type("WheelEvent", (), {"x": 500, "y": 300, "delta": 120})()

        app._canvas_wheel(event)

        self.assertEqual(preview_calls, ["preview"])
        self.assertFalse(any(delay == 16 for delay, _callback in app.root.callbacks))

    def test_segmented_beacon_warnings_do_not_create_canvas_obstacle_items(self):
        rays = [
            BeaconRay(
                angle=angle,
                reach_m=100.0,
                clear_reach_m=100.0,
                kind="clear",
                samples=[
                    BeaconRadialSample(0.0, 20.0, True),
                    BeaconRadialSample(100.0, 5.0, True),
                ],
            )
            for angle in (0.0, 2.1, 4.2)
        ]
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.beacon_profile = BeaconProfile("source", 0.0, 0.0, rays)
        app.beacon_phase = 0.5
        app.beacon_weakening_obstacles = [Obstacle(kind="Building")]
        app.beacon_blocking_obstacles = [Obstacle(kind="Building")]
        app._draw_segmented_coverage = lambda *_args, **_kwargs: True
        canvas_warning_calls: list[Obstacle] = []
        app._draw_beacon_obstacle = lambda _canvas, obstacle, *_args, **_kwargs: (
            canvas_warning_calls.append(obstacle)
        )

        app._draw_beacon(object(), draw_animation=False)

        self.assertEqual(canvas_warning_calls, [])

    def test_segmented_warning_footprint_is_clipped_to_the_ray_that_hit_it(self):
        obstacle = Obstacle(id="hit-building", x1=20, y1=-50, x2=60, y2=50)
        rays = []
        for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
            rays.append(
                BeaconRay(
                    angle,
                    100.0,
                    100.0,
                    "clear",
                    [obstacle.id] if angle == 0.0 else [],
                    [
                        BeaconRadialSample(0.0, 40.0, True),
                        BeaconRadialSample(100.0, 20.0, True),
                    ],
                )
            )
        profile = BeaconProfile("source", 0.0, 0.0, rays, max_reach_m=100.0)
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.zoom = 1.0
        app.view_x = -100.0
        app.view_y = -100.0
        app._base_scale = lambda: 1.0
        app._obstacle_bounds = lambda candidate: candidate.normalized()
        layer = Image.new("RGBA", (200, 200), (0, 0, 0, 0))

        app._draw_segmented_warning_obstacles(
            layer,
            [obstacle],
            [],
            profile=profile,
            render_scale=1,
            grow=1.0,
        )

        self.assertGreater(layer.getpixel((140, 100))[3], 0)
        self.assertEqual(layer.getpixel((140, 52))[3], 0)

    def test_zero_hop_keeps_coverage_fixed_and_animates_beacon_ripple(self):
        rays = [
            BeaconRay(
                angle,
                100.0,
                100.0,
                "clear",
                samples=[
                    BeaconRadialSample(0.0, 40.0, True),
                    BeaconRadialSample(100.0, 10.0, True),
                ],
            )
            for angle in (0.0, 2.1, 4.2)
        ]
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.static_coverage_profile = BeaconProfile("source", 0.0, 0.0, rays)
        app.static_coverage_grow = 0.42
        coverage_calls: list[float] = []
        ripple_calls: list[float] = []
        app._draw_segmented_coverage = lambda *_args, **kwargs: (
            coverage_calls.append(kwargs["grow"]) or True
        )
        app._draw_segmented_ripple = lambda _canvas, _profile, fraction: (
            ripple_calls.append(fraction) or True
        )

        app._draw_static_coverage(object())

        self.assertEqual(coverage_calls, [1.0])
        self.assertEqual(ripple_calls, [0.42])

    def test_scene_change_restarts_beacon_like_a_fresh_drop(self):
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        source = Node(id="source", name="Source")
        app.scenario = Scenario(nodes=[source])
        app.simulation_thread = None
        queued: list[tuple[str, bool, bool]] = []
        app._queue_beacon_profile = lambda node, *, keep_existing, render_on_stop=True: queued.append(
            (node.id, keep_existing, render_on_stop)
        )

        app._refresh_active_rf_after_scene_change(
            active_beacon_id=source.id,
            restart_live_mesh=False,
            restart_packet=False,
        )

        self.assertEqual(queued, [(source.id, False, False)])

    def test_scene_change_restarts_live_mesh_and_clears_its_old_packet_trace(self):
        class Toggle:
            def __init__(self) -> None:
                self.value = False

            def set(self, value: bool) -> None:
                self.value = value

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario()
        app.simulation_thread = None
        app.last_result = object()
        app.live_mesh_enabled = Toggle()
        calls: list[tuple[str, bool, bool]] = []
        app._discard_inflight_simulation = lambda: calls.append(("discard", False, False))
        app.stop_live_mesh = lambda clear_visuals=False: calls.append(("stop-live", clear_visuals, False))
        app.clear_results = lambda *, render=True, update_status=True: calls.append(
            ("clear-packet", render, update_status)
        )
        app.start_live_mesh = lambda: calls.append(("start-live", app.live_mesh_enabled.value, False))

        app._refresh_active_rf_after_scene_change(
            active_beacon_id=None,
            restart_live_mesh=True,
            restart_packet=False,
        )

        self.assertEqual(
            calls,
            [
                ("discard", False, False),
                ("stop-live", True, False),
                ("clear-packet", False, False),
                ("start-live", True, False),
            ],
        )

    def test_scene_change_requeues_standalone_packet_after_final_scene(self):
        class Root:
            def after_idle(self, callback):
                callback()

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario()
        app.simulation_thread = None
        app.last_result = object()
        app.root = Root()
        calls: list[str] = []
        app._discard_inflight_simulation = lambda: calls.append("discard")
        app.clear_results = lambda *, render=True, update_status=True: calls.append("clear")
        app.run_simulation = lambda: calls.append("run")

        app._refresh_active_rf_after_scene_change(
            active_beacon_id=None,
            restart_live_mesh=False,
            restart_packet=True,
        )

        self.assertEqual(calls, ["discard", "clear", "run"])

    def test_nearby_node_labels_are_placed_without_overlapping(self):
        placements = layout_node_labels(
            [
                ("first", "Paper test", 100.0, 100.0, False),
                ("second", "Haruki Toreda", 128.0, 106.0, False),
            ],
            canvas_width=400,
            canvas_height=300,
        )
        first = placements["first"][2:]
        second = placements["second"][2:]
        overlap_width = min(first[2], second[2]) - max(first[0], second[0])
        overlap_height = min(first[3], second[3]) - max(first[1], second[1])

        self.assertTrue(overlap_width <= 0 or overlap_height <= 0)

    def test_deep_zoom_tile_rendering_crops_to_visible_canvas(self):
        composed = Image.new("RGB", (400, 300), "black")
        tile = Image.new("RGB", (256, 256), "white")

        pasted = MeshSimulatorApp._paste_clipped_map_tile(
            composed,
            tile,
            screen_x=-50_000,
            screen_y=-40_000,
            pixel_size=100_000,
        )

        self.assertTrue(pasted)
        self.assertEqual(composed.size, (400, 300))
        self.assertEqual(composed.getpixel((200, 150)), (255, 255, 255))

    def test_source_without_receivers_retains_its_coverage_footprint(self):
        source = Node(name="Isolated source", x=500, y=500)
        unreachable = Node(name="Unreachable", x=1_000_000, y=500)
        scenario = Scenario(nodes=[source, unreachable])
        scenario.environment.stochastic = False
        result = SimulationEngine(scenario).run(
            PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=3)
        )
        first_arrivals = [
            event
            for event in result.events
            if event.kind in {"RX", "OPAQUE"} and event.hop == 1
        ]
        transmitters = [
            event.node_id
            for event in result.events
            if event.kind == "TX" and event.hop == 0
        ]
        self.assertEqual(first_arrivals, [])
        self.assertEqual(
            first_hop_coverage_to_retain(1, transmitters, first_arrivals),
            [source.id],
        )
        self.assertEqual(first_hop_coverage_to_retain(2, transmitters, first_arrivals), [])
        self.assertIn(source.id, build_coverage_contours(scenario, result))

    def test_live_node_parser_reads_meshtastic_node_database_fields(self):
        parsed = parse_live_node(
            {
                "num": 0xA1B2C3D4,
                "user": {
                    "longName": "Ridge Router",
                    "shortName": "RR",
                    "hwModel": "RAK4631",
                    "role": "ROUTER",
                },
                "position": {
                    "latitudeI": 409_123_450,
                    "longitudeI": -742_345_678,
                    "altitude": 245,
                    "altitudeSource": "ALT_INTERNAL",
                    "VDOP": 150,
                    "gpsAccuracy": 2000,
                    "precisionBits": 20,
                },
                "hopsAway": 2,
                "snr": 7.25,
                "lastHeard": 1_700_000_000,
                "isFavorite": True,
            }
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.node_num, 0xA1B2C3D4)
        self.assertEqual(parsed.name, "Ridge Router")
        self.assertEqual(parsed.role, "ROUTER")
        self.assertAlmostEqual(parsed.latitude, 40.912345)
        self.assertAlmostEqual(parsed.longitude, -74.2345678)
        self.assertEqual(parsed.altitude_m, 245)
        self.assertEqual(parsed.altitude_source, "ALT_INTERNAL")
        self.assertAlmostEqual(parsed.altitude_accuracy_m, 3.0)
        self.assertEqual(parsed.hops_away, 2)
        self.assertTrue(parsed.favorite)

    def test_live_hae_altitude_is_converted_to_msl_only_with_geoidal_separation(self):
        converted = parse_live_node(
            {
                "num": 1,
                "position": {
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "altitudeHae": 151,
                    "altitudeGeoidalSeparation": -29,
                },
            }
        )
        unconvertible = parse_live_node(
            {
                "num": 2,
                "position": {
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "altitudeHae": 151,
                },
            }
        )
        self.assertIsNotNone(converted)
        self.assertIsNotNone(unconvertible)
        self.assertEqual(converted.altitude_m, 180)
        self.assertIsNone(unconvertible.altitude_m)
        self.assertEqual(unconvertible.altitude_hae_m, 151)

    def test_nonfinite_live_altitude_and_accuracy_are_rejected(self):
        parsed = parse_live_node(
            {
                "num": 3,
                "position": {
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "altitude": float("nan"),
                    "VDOP": float("inf"),
                },
            }
        )
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.altitude_m)
        self.assertIsNone(parsed.altitude_accuracy_m)

    def test_live_radio_altitude_is_absolute_and_does_not_double_count_antenna_agl(self):
        node = Node(elevation_m=100.0, antenna_height_m=12.0, reported_altitude_m=245.0)
        self.assertTrue(node.uses_reported_altitude)
        self.assertEqual(node.antenna_z, 245.0)
        self.assertEqual(node.effective_agl_m, 145.0)
        node.use_live_altitude = False
        self.assertFalse(node.uses_reported_altitude)
        self.assertEqual(node.antenna_z, 112.0)
        self.assertEqual(node.effective_agl_m, 12.0)

    def test_impossible_live_altitude_is_corrected_without_moving_position(self):
        node = Node(
            x=1234.5,
            y=-678.9,
            elevation_m=39.08,
            antenna_height_m=2.0,
            reported_altitude_m=32.0,
            reported_altitude_accuracy_m=11.0,
        )
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app._ground_elevation_at = lambda _x, _y: 39.08

        app._apply_live_node_elevation(node)

        self.assertFalse(node.reported_altitude_usable)
        self.assertAlmostEqual(node.antenna_z, 41.08)
        self.assertEqual((node.x, node.y), (1234.5, -678.9))
        self.assertIn("below local terrain", node.reported_altitude_status)
        self.assertIn("latitude/longitude unchanged", node.reported_altitude_status)

    def test_valid_live_altitude_above_terrain_remains_authoritative(self):
        node = Node(
            elevation_m=39.08,
            antenna_height_m=2.0,
            reported_altitude_m=52.0,
        )
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app._ground_elevation_at = lambda _x, _y: 39.08

        app._apply_live_node_elevation(node)

        self.assertTrue(node.reported_altitude_usable)
        self.assertEqual(node.antenna_z, 52.0)

    def test_corrected_below_ground_altitude_still_produces_source_coverage_outline(self):
        source = Node(
            name="HarukiXL",
            elevation_m=39.08,
            antenna_height_m=2.0,
            reported_altitude_m=32.0,
        )
        scenario = Scenario(nodes=[source])
        scenario.environment.stochastic = False
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app._ground_elevation_at = lambda _x, _y: 39.08
        app._apply_live_node_elevation(source)

        result = SimulationEngine(scenario).run(
            PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=3)
        )
        points = build_coverage_contours(scenario, result)[source.id]
        radii = [math.hypot(x - source.x, y - source.y) for x, y, _kind in points]

        self.assertFalse(source.reported_altitude_usable)
        self.assertGreater(min(radii), 100.0)

    def test_live_altitude_update_clears_stale_reported_value(self):
        with_altitude = parse_live_node(
            {
                "num": 1,
                "position": {"latitude": 40.0, "longitude": -74.0, "altitude": 245},
            }
        )
        without_altitude = parse_live_node(
            {
                "num": 1,
                "position": {"latitude": 40.0, "longitude": -74.0},
            }
        )
        self.assertIsNotNone(with_altitude)
        self.assertIsNotNone(without_altitude)
        node = Node()
        MeshSimulatorApp._record_live_altitude(node, with_altitude)
        self.assertEqual(node.reported_altitude_m, 245)
        MeshSimulatorApp._record_live_altitude(node, without_altitude)
        self.assertIsNone(node.reported_altitude_m)

    def test_live_elevation_keeps_dem_ground_separate_from_radio_altitude(self):
        node = Node(
            elevation_m=0.0,
            antenna_height_m=2.0,
            reported_altitude_m=245.0,
        )
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app._ground_elevation_at = lambda _x, _y: 180.0

        app._apply_live_node_elevation(node)

        self.assertEqual(node.elevation_m, 180.0)
        self.assertEqual(node.antenna_z, 245.0)

    def test_live_node_without_shared_position_stays_unlocated(self):
        parsed = parse_live_node({"num": 123, "user": {"longName": "Private position"}})
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed.has_position)

    def test_live_reconnect_updates_existing_node_and_consolidates_duplicates(self):
        original = Node(id="original", node_num=0xDB0609E6, name="Old name", x=1, y=2)
        duplicate = Node(
            id="duplicate",
            node_num=0xDB0609E6,
            name="Duplicate",
            x=3,
            y=4,
            live_port="COM29",
        )
        scenario = Scenario(nodes=[original, duplicate])
        scenario.packet.source_id = duplicate.id
        snapshot = parse_live_node(
            {
                "num": 0xDB0609E6,
                "user": {
                    "longName": "Haruki Toreda",
                    "shortName": "HRTD",
                    "hwModel": "HELTEC_MESH_NODE_T114",
                    "role": "CLIENT",
                },
                "position": {
                    "latitude": 40.917,
                    "longitude": -74.196,
                    "altitude": 50,
                },
                "hopsAway": 0,
                "lastHeard": 1_785_281_164,
            }
        )
        self.assertIsNotNone(snapshot)

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = scenario
        app.selected_id = duplicate.id
        app.live_radio = type("RadioStub", (), {"port": "COM29"})()
        app._ground_elevation_at = lambda _x, _y: 40.0
        app.mark_dirty = lambda: None
        app._mark_results_stale = lambda: None
        app.refresh_scene_tree = lambda: None
        app._build_packet_form = lambda: None
        app._build_object_form = lambda: None
        app.render_canvas = lambda: None

        app._apply_live_nodes([snapshot], reframe=False)

        matching = [node for node in scenario.nodes if node.node_num == 0xDB0609E6]
        self.assertEqual(len(matching), 1)
        updated = matching[0]
        self.assertEqual(updated.id, duplicate.id)
        self.assertEqual(updated.name, "Haruki Toreda")
        self.assertEqual(updated.hardware_model, "HELTEC_MESH_NODE_T114")
        self.assertEqual(updated.last_heard, 1_785_281_164)
        self.assertEqual(scenario.packet.source_id, updated.id)
        self.assertEqual(app.selected_id, updated.id)

    def test_firmware_presets(self):
        self.assertEqual(PRESETS["LONG_FAST"], (250.0, 11, 5))
        self.assertEqual(PRESETS["SHORT_TURBO"], (500.0, 7, 5))
        self.assertEqual(PRESETS["LONG_MODERATE"], (125.0, 11, 8))

    def test_firmware_preset_automatically_uses_meshtastic_region_frequency(self):
        node = Node()
        self.assertEqual(node.radio.region, "US")
        self.assertEqual(node.radio.preset, "LONG_FAST")
        self.assertEqual(node.channel, "LongFast")
        self.assertAlmostEqual(node.radio.frequency_mhz, 906.875, places=6)

        node.radio.apply_preset("LONG_SLOW")

        self.assertEqual(node.radio.preset, "LONG_SLOW")
        self.assertAlmostEqual(node.radio.frequency_mhz, 905.3125, places=6)
        self.assertAlmostEqual(meshtastic_default_frequency_mhz("LONG_FAST"), 906.875, places=6)
        self.assertAlmostEqual(meshtastic_default_frequency_mhz("SHORT_TURBO"), 926.75, places=6)

        node.radio.apply_region("EU_868", "LongSlow")
        self.assertEqual(node.radio.region, "EU_868")
        self.assertEqual(node.radio.preset, "LONG_SLOW")
        self.assertAlmostEqual(node.radio.frequency_mhz, 869.4625, places=6)

        node.radio.apply_region("EU_866")
        self.assertEqual(node.radio.region, "EU_866")
        self.assertEqual(node.radio.preset, "LITE_FAST")
        self.assertAlmostEqual(node.radio.frequency_mhz, 866.3, places=6)

    def test_region_band_plans_cover_wide_lora_and_fixed_amateur_slots(self):
        self.assertIn("LORA_24", REGION_BANDS)
        self.assertEqual(preset_parameters("LONG_FAST", "LORA_24"), (812.5, 11, 5))
        self.assertAlmostEqual(
            meshtastic_default_frequency_mhz("LONG_FAST", region="LORA_24"),
            2420.71875,
            places=6,
        )
        self.assertAlmostEqual(
            meshtastic_default_frequency_mhz("TINY_FAST", region="ITU2_2M"),
            145.01,
            places=6,
        )

    def test_region_frequency_is_used_by_propagation_and_radio_compatibility(self):
        low_source = Node(x=0, y=0)
        low_target = Node(x=1000, y=0)
        low_source.radio.apply_region("EU_433")
        low_target.radio.apply_region("EU_433")
        low_model = PropagationModel(Scenario(nodes=[low_source, low_target]))

        high_source = Node(x=0, y=0)
        high_target = Node(x=1000, y=0)
        high_source.radio.apply_region("LORA_24")
        high_target.radio.apply_region("LORA_24")
        high_model = PropagationModel(Scenario(nodes=[high_source, high_target]))

        self.assertGreater(low_model.link(low_source, low_target).rssi_dbm, high_model.link(high_source, high_target).rssi_dbm)
        self.assertEqual(
            PropagationModel.radios_compatible(low_source, high_target),
            (False, "frequency mismatch"),
        )

    def test_preset_preview_updates_default_channel_and_frequency_but_preserves_custom_name(self):
        class Value:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.object_vars = {
            "region": Value("US"),
            "preset": Value("LONG_SLOW"),
            "channel": Value("LongFast"),
            "frequency_mhz": Value("906.875"),
            "bandwidth_khz": Value("250"),
            "spreading_factor": Value("11"),
            "coding_rate": Value("5"),
        }

        app._preset_preview()

        self.assertEqual(app.object_vars["channel"].get(), "LongSlow")
        self.assertEqual(app.object_vars["frequency_mhz"].get(), "905.3125")

        app.object_vars["preset"].set("MEDIUM_FAST")
        app.object_vars["channel"].set("NeighborhoodPrivate")
        app._preset_preview()

        self.assertEqual(app.object_vars["channel"].get(), "NeighborhoodPrivate")
        self.assertEqual(
            float(app.object_vars["frequency_mhz"].get()),
            meshtastic_default_frequency_mhz("MEDIUM_FAST", "NeighborhoodPrivate"),
        )

        app.object_vars["channel"].set("MediumFast")
        app.object_vars["region"].set("EU_866")
        app._region_preview()

        self.assertEqual(app.object_vars["preset"].get(), "LITE_FAST")
        self.assertEqual(app.object_vars["channel"].get(), "LiteFast")
        self.assertAlmostEqual(float(app.object_vars["frequency_mhz"].get()), 866.3, places=6)

        app.object_vars["preset"].set("NARROW_SLOW")
        app._preset_preview()

        self.assertEqual(app.object_vars["region"].get(), "EU_N_868")
        self.assertAlmostEqual(float(app.object_vars["frequency_mhz"].get()), 869.44165, places=6)

    def test_airtime_slow_is_longer(self):
        fast = Node()
        fast.radio.apply_preset("SHORT_FAST")
        slow = Node()
        slow.radio.apply_preset("LONG_SLOW")
        self.assertGreater(PropagationModel.airtime_ms(slow, 32), PropagationModel.airtime_ms(fast, 32))

    def test_unobstructed_range_ends_at_field_decode_threshold(self):
        source = Node(x=0, y=0)
        receiver = Node(x=1, y=0)
        scenario = Scenario(nodes=[source, receiver])
        scenario.environment.stochastic = False
        model = PropagationModel(scenario)
        receiver.x = model.unobstructed_range_m(source, receiver)
        self.assertAlmostEqual(
            model.link(source, receiver).margin_db,
            MIN_DECODE_MARGIN_DB,
            delta=0.05,
        )

    def test_packet_delivery_uses_same_field_threshold_as_coverage(self):
        source = Node(x=0, y=0)
        receiver = Node(x=1, y=0)
        scenario = Scenario(nodes=[source, receiver])
        scenario.environment.stochastic = False
        model = PropagationModel(scenario)
        receiver.x = model.unobstructed_range_m(source, receiver) * 0.99

        link = model.link(source, receiver)
        result = SimulationEngine(scenario).run(
            PacketConfig(source_id=source.id, destination_id=receiver.id)
        )

        self.assertGreaterEqual(link.margin_db, MIN_DECODE_MARGIN_DB)
        self.assertIn(receiver.id, result.reached)

    def test_obstacle_adds_loss(self):
        a = Node(x=0, y=0, antenna_height_m=2)
        b = Node(x=1000, y=0, antenna_height_m=2)
        scenario = Scenario(nodes=[a, b])
        clear = PropagationModel(scenario).link(a, b)
        scenario.obstacles.append(
            Obstacle(x1=400, y1=-100, x2=600, y2=100, height_m=20, attenuation_db=20, loss_per_100m_db=0)
        )
        blocked = PropagationModel(scenario).link(a, b)
        self.assertAlmostEqual(clear.rssi_dbm - blocked.rssi_dbm, 20, places=3)

    def test_beacon_samples_keep_cumulative_building_loss_and_attribute_each_hit(self):
        source = Node(x=0, y=0, antenna_height_m=2)
        buildings = [
            Obstacle(
                id=f"building-{index}",
                name=f"Building {index}",
                x1=distance,
                y1=-20,
                x2=distance + 20,
                y2=20,
                height_m=20,
                attenuation_db=6,
                loss_per_100m_db=0,
            )
            for index, distance in enumerate((180, 380, 580), start=1)
        ]
        model = PropagationModel(Scenario(nodes=[source], obstacles=buildings))

        profile = model.beacon_profile(
            source,
            angular_samples=8,
            max_range_m=1_000,
            segment_samples=40,
        )
        east = profile.rays[0]
        losses = [
            next(sample.obstacle_loss_db for sample in east.samples if sample.distance_m >= distance)
            for distance in (250, 450, 650)
        ]

        self.assertEqual(losses, [6.0, 12.0, 18.0])
        self.assertEqual(set(east.obstacle_ids), {building.id for building in buildings})
        self.assertTrue({building.id for building in buildings}.issubset(profile.weakening_obstacle_ids))

    def test_large_import_spatial_index_keeps_intersecting_obstacles(self):
        source = Node(x=0, y=500)
        target = Node(x=10_000, y=500)
        crossing = Obstacle(
            name="Crossing building",
            kind="Building",
            x1=4_900,
            y1=450,
            x2=5_100,
            y2=550,
            attenuation_db=18,
        )
        distant = [
            Obstacle(
                name=f"Distant {index}",
                kind="Building",
                x1=20_000 + index * 40,
                y1=20_000,
                x2=20_020 + index * 40,
                y2=20_020,
            )
            for index in range(200)
        ]
        model = PropagationModel(
            Scenario(
                environment=Environment(initial_view_width_m=40_000, initial_view_height_m=30_000),
                nodes=[source, target],
                obstacles=[crossing, *distant],
            )
        )
        candidates = model._candidate_obstacles(source, target)
        self.assertIn(crossing, candidates)
        self.assertLess(len(candidates), len(distant))
        loss, names = model.obstacle_loss(source, target)
        self.assertGreater(loss, 10)
        self.assertTrue(any("Crossing building" in name for name in names))

    def test_high_antennas_clear_building(self):
        a = Node(x=0, y=0, antenna_height_m=100)
        b = Node(x=1000, y=0, antenna_height_m=100)
        obs = Obstacle(x1=400, y1=-100, x2=600, y2=100, height_m=10, attenuation_db=30)
        scenario = Scenario(nodes=[a, b], obstacles=[obs])
        loss, _ = PropagationModel(scenario).obstacle_loss(a, b)
        self.assertEqual(loss, 0)

    def test_mountain_triangle_hard_blocks_low_path(self):
        a = Node(x=0, y=0, antenna_height_m=2)
        b = Node(x=1500, y=0, antenna_height_m=2)
        mountain = Obstacle(
            name="Ridge",
            kind="Mountain",
            x1=400,
            y1=-200,
            x2=600,
            y2=200,
            height_m=180,
            behavior="BLOCK",
            max_range_beyond_m=0,
        )
        link = PropagationModel(Scenario(nodes=[a, b], obstacles=[mountain])).link(a, b)
        self.assertFalse(link.compatible)
        self.assertEqual(link.probability, 0)
        self.assertIn("blocks", link.reason)

    def test_high_path_clears_mountain(self):
        a = Node(x=0, y=0, antenna_height_m=300)
        b = Node(x=1500, y=0, antenna_height_m=300)
        mountain = Obstacle(
            kind="Mountain",
            x1=400,
            y1=-200,
            x2=600,
            y2=200,
            height_m=100,
            behavior="BLOCK",
        )
        link = PropagationModel(Scenario(nodes=[a, b], obstacles=[mountain])).link(a, b)
        self.assertTrue(link.compatible)

    def test_building_limits_travel_to_point_three_miles_beyond(self):
        a = Node(x=0, y=0, antenna_height_m=2)
        near = Node(x=900, y=0, antenna_height_m=2)
        far = Node(x=1100, y=0, antenna_height_m=2)
        building = Obstacle(
            name="Building",
            kind="Building",
            x1=400,
            y1=-100,
            x2=500,
            y2=100,
            height_m=20,
            behavior="LIMIT_AFTER",
            max_range_beyond_m=482.803,
        )
        model = PropagationModel(Scenario(nodes=[a, near, far], obstacles=[building]))
        self.assertTrue(model.link(a, near).compatible)
        far_link = model.link(a, far)
        self.assertFalse(far_link.compatible)
        self.assertIn("0.30 mi", far_link.reason)

    def test_painted_forest_adds_distance_loss(self):
        a = Node(x=0, y=0, antenna_height_m=2)
        b = Node(x=1000, y=0, antenna_height_m=2)
        forest = Obstacle(
            kind="Forest",
            shape="brush",
            points=[[500, -200], [500, 200]],
            brush_radius_m=100,
            height_m=20,
            attenuation_db=2,
            loss_per_100m_db=10,
            behavior="ATTENUATE",
        )
        scenario = Scenario(nodes=[a, b], obstacles=[forest])
        loss, names = PropagationModel(scenario).obstacle_loss(a, b)
        self.assertGreater(loss, 10)
        self.assertTrue(names)

    def test_coverage_link_fails_on_low_ground_and_recovers_on_high_terrain(self):
        source = Node(x=0, y=0, elevation_m=0, antenna_height_m=1.5)
        building = Obstacle(
            kind="Building",
            x1=100,
            y1=-25,
            x2=140,
            y2=25,
            base_elevation_m=0,
            height_m=20,
            attenuation_db=80,
            behavior="ATTENUATE",
        )
        model = PropagationModel(Scenario(nodes=[source], obstacles=[building]))
        low_ground = Node(x=400, y=0, elevation_m=0, antenna_height_m=1.5)
        high_terrain = Node(x=800, y=0, elevation_m=200, antenna_height_m=1.5)

        self.assertLess(model.link(source, low_ground).margin_db, 0)
        self.assertGreater(model.link(source, high_terrain).margin_db, 0)

    def test_coverage_keeps_a_small_red_intermittent_fringe(self):
        source = Node(x=0, y=0)
        model = PropagationModel(Scenario(nodes=[source]))

        profile = model.beacon_profile(source, angular_samples=8, segment_samples=32)
        samples = profile.rays[0].samples
        edge = [sample for sample in samples if sample.reachable][-1]

        self.assertLess(edge.margin_db, 0.0)
        self.assertGreaterEqual(edge.margin_db, MIN_DECODE_MARGIN_DB)
        self.assertTrue(edge.reachable)
        self.assertLess(samples[-1].margin_db, MIN_DECODE_MARGIN_DB)
        self.assertFalse(samples[-1].reachable)

    def test_old_scenario_migrates_obstacle_behavior(self):
        scenario = Scenario.from_dict(
            {
                "obstacles": [
                    {"name": "Old ridge", "kind": "Mountain"},
                    {"name": "Old building", "kind": "Building"},
                ]
            }
        )
        self.assertEqual(scenario.obstacles[0].behavior, "BLOCK")
        self.assertEqual(scenario.obstacles[1].behavior, "ATTENUATE")
        self.assertEqual(scenario.obstacles[1].max_range_beyond_m, 0.0)

    def test_exact_old_building_preset_migrates_without_range_cap(self):
        scenario = Scenario.from_dict(
            {
                "obstacles": [
                    {
                        "kind": "Building",
                        "attenuation_db": 18.0,
                        "loss_per_100m_db": 0.3,
                        "behavior": "LIMIT_AFTER",
                        "max_range_beyond_m": 482.803,
                    }
                ]
            }
        )
        building = scenario.obstacles[0]
        self.assertEqual(building.behavior, "ATTENUATE")
        self.assertEqual(building.max_range_beyond_m, 0.0)
        self.assertAlmostEqual(building.attenuation_db, 10.8)
        self.assertAlmostEqual(building.loss_per_100m_db, 0.3)

    def test_building_calibration_does_not_change_propagation_baseline(self):
        restored = Scenario.from_dict({"environment": {"path_loss_exponent": 2.45}})
        self.assertEqual(restored.environment.path_loss_exponent, 2.45)
        self.assertEqual(Environment().path_loss_exponent, 2.45)

    def test_survey_calibration_updates_every_building_and_local_distance_loss(self):
        buildings = [Obstacle(kind="Building"), Obstacle(kind="Building", attenuation_db=14)]
        wall = Obstacle(kind="Wall", attenuation_db=25, behavior="BLOCK")
        scenario = Scenario(obstacles=[*buildings, wall])
        calibration = BuildingCalibration(
            sample_count=133,
            received_sample_count=100,
            lost_sample_count=33,
            clear_sample_count=66,
            obstructed_sample_count=67,
            building_count=2,
            penetration_db=2.59,
            loss_per_100m_db=0.29,
            path_loss_exponent=3.37,
            calibration_offset_db=-5.23,
            fitted_rmse_db=7.33,
        )

        changed = apply_building_calibration(scenario, calibration)

        self.assertEqual(changed, 2)
        for building in buildings:
            self.assertEqual(building.attenuation_db, 2.59)
            self.assertEqual(building.loss_per_100m_db, 0.29)
            self.assertEqual(building.behavior, "ATTENUATE")
            self.assertEqual(building.max_range_beyond_m, 0.0)
        self.assertEqual(wall.behavior, "BLOCK")
        self.assertEqual(scenario.environment.path_loss_exponent, 2.45)

    def test_dense_building_maps_keep_the_original_beacon_sampling(self):
        self.assertEqual(MeshSimulatorApp._beacon_ray_count(2_315), 72)

    def test_geographic_projection_roundtrip(self):
        x, y = latlon_to_world(40.7138, -74.004, 40.7128, -74.006)
        latitude, longitude = world_to_latlon(x, y, 40.7128, -74.006)
        self.assertAlmostEqual(latitude, 40.7138, places=6)
        self.assertAlmostEqual(longitude, -74.004, places=6)

    def test_geographic_projection_is_center_relative_and_has_no_world_extent(self):
        projected = latlon_to_world(40.7138, -74.004, 40.7128, -74.006)
        self.assertNotEqual(projected, (0.0, 0.0))
        self.assertEqual(
            latlon_to_world(40.7128, -74.006, 40.7128, -74.006),
            (0.0, 0.0),
        )

    def test_legacy_rectangular_coordinates_migrate_without_moving_geography(self):
        restored = Scenario.from_dict(
            {
                "environment": {
                    "width_m": 1_000,
                    "height_m": 500,
                    "map_center_lat": 40.7128,
                    "map_center_lon": -74.006,
                    "terrain_columns": 2,
                    "terrain_rows": 2,
                    "terrain_values": [1.0, 2.0, 3.0, 4.0],
                },
                "nodes": [{"x": 700.0, "y": 200.0}],
                "obstacles": [
                    {
                        "x1": 600.0,
                        "y1": 100.0,
                        "x2": 800.0,
                        "y2": 300.0,
                        "points": [[600.0, 100.0], [800.0, 300.0]],
                    }
                ],
            }
        )
        self.assertEqual(restored.environment.coordinate_space, "CENTERED_MERCATOR")
        self.assertEqual((restored.nodes[0].x, restored.nodes[0].y), (200.0, -50.0))
        self.assertEqual(restored.obstacles[0].points[0], [100.0, -150.0])
        self.assertEqual(
            restored.environment.terrain_bounds(),
            (-500.0, -250.0, 500.0, 250.0),
        )

    def test_terrain_cache_can_cover_negative_unbounded_coordinates(self):
        environment = Environment(
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[10.0, 20.0, 30.0, 40.0],
            terrain_left_m=-20_000.0,
            terrain_top_m=-15_000.0,
            terrain_width_m=2_000.0,
            terrain_height_m=1_000.0,
        )
        self.assertAlmostEqual(environment.ground_elevation(-19_000.0, -14_500.0), 25.0)
        self.assertIsNone(environment.ground_elevation(-17_999.0, -14_500.0))

    def test_new_scenario_serializes_view_span_not_rf_world_dimensions(self):
        environment_data = Scenario().to_dict()["environment"]
        self.assertNotIn("width_m", environment_data)
        self.assertNotIn("height_m", environment_data)
        self.assertIn("initial_view_width_m", environment_data)
        self.assertIn("initial_view_height_m", environment_data)

    def test_node_drag_is_not_clamped_to_any_rectangle(self):
        node = Node(x=0.0, y=0.0)
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(nodes=[node])
        app.tool = "select"
        app.drag_start_world = (0.0, 0.0)
        app.drag_object_origin = (0.0, 0.0)
        app.drag_object_points = None
        app.get_selected = lambda: node
        app.screen_to_world = lambda _x, _y: (12_000_000.0, -9_000_000.0)
        app._set_auto_node_elevation = lambda _node: False
        app._render_simulation_layers = lambda: None
        event = type("Event", (), {"x": 10, "y": 10})()

        app._canvas_drag(event)

        self.assertEqual((node.x, node.y), (12_000_000.0, -9_000_000.0))

    def test_terrain_request_coverage_includes_far_nodes_without_expanding_world(self):
        far_node = Node(x=2_000_000.0, y=-1_500_000.0)
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(nodes=[far_node])
        app.canvas = type(
            "Canvas",
            (),
            {"winfo_width": lambda _self: 1000, "winfo_height": lambda _self: 700},
        )()
        app.screen_to_world = lambda x, y: (
            -5_000.0 if x == 0 else 5_000.0,
            -3_500.0 if y == 0 else 3_500.0,
        )

        left, top, right, bottom = app._terrain_request_bounds()

        self.assertLess(left, -5_000.0)
        self.assertLess(top, far_node.y)
        self.assertGreater(right, far_node.x)
        self.assertGreater(bottom, 3_500.0)

    def test_zoomed_out_map_viewport_is_not_clipped_to_scenario_bounds(self):
        left, top, right, bottom = world_viewport_to_mercator_bounds(
            -15_000,
            -8_000,
            25_000,
            18_000,
            40.9,
            -74.1,
        )
        self.assertAlmostEqual(right - left, 40_000)
        self.assertAlmostEqual(top - bottom, 26_000)

    def test_polygon_obstacle_intersects_real_outline(self):
        a = Node(x=0, y=500, antenna_height_m=2)
        b = Node(x=1000, y=500, antenna_height_m=2)
        building = Obstacle(
            kind="Building",
            shape="polygon",
            points=[[400, 400], [600, 400], [600, 600], [400, 600], [400, 400]],
            height_m=20,
            behavior="LIMIT_AFTER",
            max_range_beyond_m=100,
        )
        link = PropagationModel(Scenario(nodes=[a, b], obstacles=[building])).link(a, b)
        self.assertFalse(link.compatible)
        self.assertIn("limits travel", link.reason)

    def test_topography_blocks_line_of_sight(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=100,
            terrain_columns=5,
            terrain_rows=2,
            terrain_values=[0, 0, 100, 0, 0, 0, 0, 100, 0, 0],
            terrain_source="test ridge",
        )
        a = Node(x=0, y=50, elevation_m=0, antenna_height_m=2)
        b = Node(x=1000, y=50, elevation_m=0, antenna_height_m=2)
        link = PropagationModel(Scenario(environment=environment, nodes=[a, b])).link(a, b)
        self.assertFalse(link.compatible)
        self.assertIn("Topography blocks", link.reason)

    def test_auto_ground_offset_does_not_false_block_without_obstacles(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            stochastic=False,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[100.0, 100.0, 100.0, 100.0],
        )
        source = Node(x=400, y=500, elevation_m=90.0, antenna_height_m=2.0)
        target = Node(x=500, y=500, elevation_m=90.0, antenna_height_m=2.0)
        scenario = Scenario(environment=environment, nodes=[source, target], obstacles=[])

        link = PropagationModel(scenario).link(source, target)

        self.assertTrue(link.compatible, link.reason)
        self.assertNotIn("Topography blocks", link.reason)

    def test_higher_resolution_endpoint_does_not_create_a_synthetic_terrain_slope(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            stochastic=False,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[100.0, 100.0, 100.0, 100.0],
        )
        source = Node(x=0, y=500, elevation_m=100.0, antenna_height_m=2.0)
        target = Node(x=1000, y=500, elevation_m=124.0, antenna_height_m=2.0)
        model = PropagationModel(Scenario(environment=environment, nodes=[source, target]))

        terrain_loss, blocked_reason = model._terrain_effects(source, target)

        self.assertEqual(terrain_loss, 0.0)
        self.assertEqual(blocked_reason, "")

    def test_beacon_coverage_sweep_snaps_to_a_real_receiver_s_own_elevation(self):
        """The bounded RF terrain grid is far coarser than a surveyed node's own
        elevation reading.  A ray that reaches all the way to that node's exact
        position must judge clearance using ITS elevation, not the coarse grid
        cell underneath it -- otherwise the coverage heatmap and the direct
        node-to-node link disagree over the exact same spot."""
        environment = Environment(
            initial_view_width_m=2000,
            initial_view_height_m=1000,
            stochastic=False,
            terrain_columns=3,
            terrain_rows=2,
            # A modest ridge sits halfway between the two nodes. Grid elevation
            # at the receiver's own column reads 0 m -- 5 m below what the node
            # itself recorded on placement.
            terrain_values=[8.0, 7.0, 0.0, 8.0, 7.0, 0.0],
        )
        source = Node(x=0, y=500, elevation_m=8.0, antenna_height_m=2.0)
        target = Node(x=2000, y=500, elevation_m=5.0, antenna_height_m=2.0)
        scenario = Scenario(environment=environment, nodes=[source, target])
        model = PropagationModel(scenario)

        # Using the receiver's own (higher) elevation, the ridge is cleared.
        terrain_loss, blocked_reason = model._terrain_effects(source, target)
        self.assertEqual(blocked_reason, "")

        # The coverage sweep, capped exactly at the receiver's own distance,
        # must reach it rather than reporting the ridge as blocking -- which is
        # what the coarse grid's own elevation at that spot (0 m) would do.
        profile = model.beacon_profile(source, angular_samples=8, max_range_m=2000)
        east_ray = min(profile.rays, key=lambda ray: abs(ray.angle))
        self.assertEqual(east_ray.kind, "clear")
        self.assertAlmostEqual(east_ray.reach_m, 2000.0, delta=1.0)

    def test_manual_below_ground_override_remains_physically_blocked(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            stochastic=False,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[100.0, 100.0, 100.0, 100.0],
        )
        source = Node(
            x=400,
            y=500,
            elevation_m=90.0,
            elevation_override=True,
            antenna_height_m=2.0,
        )
        target = Node(
            x=500,
            y=500,
            elevation_m=90.0,
            elevation_override=True,
            antenna_height_m=2.0,
        )

        link = PropagationModel(
            Scenario(environment=environment, nodes=[source, target], obstacles=[])
        ).link(source, target)

        self.assertFalse(link.compatible)
        self.assertIn("Topography blocks", link.reason)

    def test_source_only_coverage_ripple_survives_dem_grid_offset(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            stochastic=False,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[100.0, 100.0, 100.0, 100.0],
        )
        source = Node(x=500, y=500, elevation_m=90.0, antenna_height_m=2.0)
        scenario = Scenario(environment=environment, nodes=[source], obstacles=[])
        packet = PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=3)

        result = SimulationEngine(scenario).run(packet)
        contour = build_coverage_contours(scenario, result)[source.id]
        radii = [math.hypot(x - source.x, y - source.y) for x, y, _kind in contour]

        self.assertEqual(result.transmissions, 1)
        self.assertGreater(min(radii), 100.0)

    def test_terrain_only_visual_contains_hillshade_and_contours(self):
        visual = build_terrain_visual(
            5,
            5,
            [
                0, 10, 20, 30, 40,
                5, 20, 40, 55, 60,
                10, 35, 80, 70, 65,
                5, 20, 45, 55, 60,
                0, 10, 20, 30, 40,
            ],
            1000,
            1000,
        )
        self.assertIsNotNone(visual)
        assert visual is not None
        self.assertEqual(visual.mode, "RGB")
        self.assertGreaterEqual(max(visual.size), 768)
        self.assertGreater(len(set(visual.convert("L").tobytes())), 2)
        self.assertIsNone(build_terrain_visual(0, 0, [], 1000, 1000))

    def test_terrain_only_view_decodes_exact_dem_elevations(self):
        tile = Image.new("RGB", (2, 1))
        tile.putdata([(128, 123, 128), (127, 255, 0)])
        encoded = io.BytesIO()
        tile.save(encoded, format="PNG")
        elevations = decode_terrarium_elevations(encoded.getvalue())
        self.assertAlmostEqual(float(elevations[0, 0]), 123.5, places=2)
        self.assertAlmostEqual(float(elevations[0, 1]), -1.0, places=2)

    def test_dem_tile_sampling_interpolates_the_ground_under_a_node(self):
        elevations = np.asarray([[100.0, 200.0], [300.0, 500.0]], dtype=np.float32)
        self.assertAlmostEqual(sample_elevation_array(elevations, 12.125, 9.25), 237.5)

    def test_auto_node_elevation_uses_ground_even_when_rf_terrain_is_disabled(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            terrain_enabled=False,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[100.0, 200.0, 300.0, 500.0],
        )
        automatic = Node(x=250, y=500, elevation_m=0)
        manual = Node(x=250, y=500, elevation_m=777, elevation_override=True)
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(environment=environment, nodes=[automatic, manual])
        app.map_tile_bytes = {}
        app.terrain_tile_elevations = {}
        app.map_tile_failures = set()

        self.assertTrue(app._set_auto_node_elevation(automatic))
        self.assertAlmostEqual(automatic.elevation_m, 237.5)
        self.assertFalse(app._set_auto_node_elevation(manual))
        self.assertEqual(manual.elevation_m, 777)

    def test_cached_exact_dem_takes_precedence_over_coarse_scenario_terrain(self):
        environment = Environment(
            initial_view_width_m=1000,
            initial_view_height_m=1000,
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[10.0, 10.0, 10.0, 10.0],
        )
        node = Node(x=500, y=500)
        zoom = 12
        center_x, center_y = latlon_to_mercator(
            environment.map_center_lat,
            environment.map_center_lon,
        )
        tile_x_float, tile_y_float = mercator_to_tile(center_x, center_y, zoom)
        tile_x, tile_y = math.floor(tile_x_float), math.floor(tile_y_float)
        terrarium = Image.new("RGB", (2, 2), (128, 123, 128))
        encoded = io.BytesIO()
        terrarium.save(encoded, format="PNG")
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(environment=environment, nodes=[node])
        app.map_tile_bytes = {("TerrainDEM", zoom, tile_x, tile_y): encoded.getvalue()}
        app.terrain_tile_elevations = {}
        app.map_tile_failures = set()

        self.assertTrue(app._set_auto_node_elevation(node))
        self.assertAlmostEqual(node.elevation_m, 123.5, places=2)

    def test_node_elevation_override_roundtrips_with_scenario(self):
        node = Node(elevation_m=432.1, elevation_override=True)
        restored = Scenario.from_json(Scenario(nodes=[node]).to_json())
        self.assertTrue(restored.nodes[0].elevation_override)
        self.assertAlmostEqual(restored.nodes[0].elevation_m, 432.1)

    def test_geospatial_scenario_roundtrip(self):
        environment = Environment(
            map_configured=True,
            map_center_lat=40.7484421,
            map_center_lon=-73.9856589,
            map_layer="Topographic",
            terrain_columns=2,
            terrain_rows=2,
            terrain_values=[10.0, 11.0, 12.0, 13.0],
            terrain_source="Mapzen/AWS test",
        )
        obstacle = Obstacle(
            kind="Building",
            shape="polygon",
            points=[[1, 1], [2, 1], [2, 2], [1, 1]],
            osm_id="way/123",
            base_elevation_m=11,
        )
        restored = Scenario.from_json(Scenario(environment=environment, obstacles=[obstacle]).to_json())
        self.assertTrue(restored.environment.map_configured)
        self.assertEqual(restored.environment.terrain_values, [10.0, 11.0, 12.0, 13.0])
        self.assertEqual(restored.obstacles[0].osm_id, "way/123")
        self.assertEqual(restored.obstacles[0].shape, "polygon")

    def test_blank_scenario_starts_with_a_visible_geographic_basemap(self):
        environment = Scenario().environment
        self.assertTrue(environment.map_configured)
        self.assertNotEqual((environment.map_center_lat, environment.map_center_lon), (0.0, 0.0))

    def test_legacy_blank_scenario_is_migrated_to_default_basemap(self):
        restored = Scenario.from_dict(
            {
                "name": "Old blank map",
                "environment": {
                    "map_configured": False,
                    "map_center_lat": 0.0,
                    "map_center_lon": 0.0,
                },
            }
        )
        self.assertTrue(restored.environment.map_configured)
        self.assertNotEqual(
            (restored.environment.map_center_lat, restored.environment.map_center_lon),
            (0.0, 0.0),
        )

    def test_map_tiles_are_eink_style_grayscale(self):
        source = Image.new("RGB", (2, 1))
        source.putdata([(255, 0, 0), (0, 128, 255)])
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        converted = grayscale_map_tile(buffer.getvalue(), 4)
        self.assertEqual(converted.mode, "L")
        self.assertEqual(converted.size, (4, 4))
        minimum, maximum = converted.getextrema()
        self.assertLess(minimum, maximum)

    def test_resized_map_tile_decodes_once_across_a_continuous_zoom(self):
        """A continuous zoom asks for a new pixel size on nearly every tick.
        Re-running the full decode/contrast-enhance pipeline for each one was
        slow enough to flash the bare canvas background between the old and
        new raster on the settle-render. Only the resize should repeat."""
        source = Image.new("RGB", (2, 1))
        source.putdata([(255, 0, 0), (0, 128, 255)])
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        data = buffer.getvalue()
        key = ("Topographic", 10, 5, 5)

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.map_tile_decoded = {}

        first = app._resized_map_tile(key, data, 4)
        second = app._resized_map_tile(key, data, 6)

        self.assertEqual(len(app.map_tile_decoded), 1)
        self.assertEqual(first.size, (4, 4))
        self.assertEqual(second.size, (6, 6))

    def test_hidden_map_uses_a_light_neutral_canvas(self):
        class Hidden:
            @staticmethod
            def get():
                return False

        class Canvas:
            @staticmethod
            def winfo_width():
                return 24

            @staticmethod
            def winfo_height():
                return 16

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.map_visible = Hidden()
        app.scenario = Scenario()

        layer = app._compose_map_layer(Canvas())

        expected = ImageColor.getrgb(MAPLESS_BACKGROUND)
        self.assertEqual(layer.getpixel((0, 0)), expected)
        self.assertEqual(layer.getpixel((23, 15)), expected)

    def test_overture_buildings_convert_polygon_and_height_metadata(self):
        first = Polygon(
            [
                (-74.2100, 40.9040),
                (-74.2098, 40.9040),
                (-74.2098, 40.9042),
                (-74.2100, 40.9040),
            ]
        )
        second = MultiPolygon(
            [
                Polygon(
                    [
                        (-74.2096, 40.9040),
                        (-74.2094, 40.9040),
                        (-74.2094, 40.9042),
                        (-74.2096, 40.9040),
                    ]
                ),
                Polygon(
                    [
                        (-74.2092, 40.9040),
                        (-74.2090, 40.9040),
                        (-74.2090, 40.9042),
                        (-74.2092, 40.9040),
                    ]
                ),
            ]
        )
        elements = overture_rows_to_elements(
            [
                {
                    "id": "building-one",
                    "geometry": first.wkb,
                    "height": 8.25,
                    "num_floors": 2,
                    "names": {"primary": "Test Hall"},
                    "sources": [{"dataset": "Microsoft ML Buildings"}],
                },
                {
                    "id": "building-two",
                    "geometry": second.wkb,
                    "height": None,
                    "num_floors": None,
                    "names": None,
                    "sources": [{"dataset": "OpenStreetMap"}],
                },
            ]
        )
        self.assertEqual(len(elements), 3)
        self.assertEqual(elements[0]["type"], "overture")
        self.assertEqual(elements[0]["tags"]["name"], "Test Hall")
        self.assertEqual(elements[0]["tags"]["height"], "8.250")
        self.assertEqual(elements[0]["tags"]["building:levels"], "2")
        self.assertIn("Microsoft ML Buildings", elements[0]["tags"]["source:datasets"])
        self.assertAlmostEqual(elements[0]["geometry"][0]["lon"], -74.2100)
        self.assertAlmostEqual(elements[0]["geometry"][0]["lat"], 40.9040)
        self.assertEqual(elements[1]["id"], "building-two")
        self.assertEqual(elements[2]["id"], "building-two:1")

    def test_overture_building_conversion_honors_feature_cap(self):
        footprint = Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])
        rows = [{"id": str(index), "geometry": footprint.wkb} for index in range(5)]
        self.assertEqual(len(overture_rows_to_elements(rows, limit=2)), 2)

    def test_obstacle_query_tiles_cover_complete_visible_bounds(self):
        cells = split_geographic_bounds(40.0, -75.0, 42.0, -71.0, columns=2, rows=2)
        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[0], (40.0, -75.0, 41.0, -73.0))
        self.assertEqual(cells[-1], (41.0, -73.0, 42.0, -71.0))
        self.assertEqual(min(cell[0] for cell in cells), 40.0)
        self.assertEqual(max(cell[2] for cell in cells), 42.0)
        self.assertEqual(min(cell[1] for cell in cells), -75.0)
        self.assertEqual(max(cell[3] for cell in cells), -71.0)

    def test_obstacle_import_plan_within_area_cap(self):
        small = obstacle_import_plan(4_000, 3_000)
        self.assertEqual(OBSTACLE_IMPORT_MAX_AREA_M2, 12_000_000.0)
        self.assertEqual(small[:2], (2, 2))
        self.assertFalse(small[3])

    def test_saturated_obstacle_cells_are_subdivided(self):
        service = MapDataService.__new__(MapDataService)
        queried: list[tuple[float, float, float, float]] = []

        def fake_fetch(
            south: float,
            west: float,
            north: float,
            east: float,
            *,
            limit: int,
        ) -> list[dict[str, object]]:
            queried.append((south, west, north, east))
            if (north - south) * (east - west) > 0.1:
                return [
                    {"type": "overture", "id": f"saturated-{index}", "geometry": []}
                    for index in range(limit)
                ]
            return [
                {
                    "type": "overture",
                    "id": f"{south:.3f}-{west:.3f}",
                    "geometry": [],
                }
            ]

        service.fetch_overture_buildings = fake_fetch  # type: ignore[method-assign]
        progress: list[tuple[int, int, str]] = []
        elements = service.fetch_overture_buildings_for_viewport(
            0.0,
            0.0,
            1.0,
            1.0,
            limit=100,
            columns=1,
            rows=1,
            progress_callback=lambda completed, total, phase: progress.append((completed, total, phase)),
        )
        self.assertGreater(len(queried), 1)
        self.assertEqual(len(elements), 16)
        self.assertTrue(any((north - south) <= 0.25 for south, _west, north, _east in queried))
        fractions = [completed / total for completed, total, _phase in progress]
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)

    def test_country_scale_terrain_uses_coarse_bounded_tile_set(self):
        encoded = Image.new("RGB", (256, 256), (128, 0, 0))
        buffer = io.BytesIO()
        encoded.save(buffer, format="PNG")
        requested: set[tuple[int, int, int]] = set()
        service = MapDataService.__new__(MapDataService)

        def fake_tile(_layer, zoom, x, y):
            requested.add((zoom, x, y))
            return buffer.getvalue()

        service.get_tile_bytes = fake_tile
        columns, rows, values, zoom = service.build_terrain_grid(39.0, -98.0, 4_000_000, 3_000_000, columns=9)
        self.assertLessEqual(zoom, 7)
        self.assertLessEqual(len(requested), 36)
        self.assertEqual(len(values), columns * rows)

    def test_router_bridges_line(self):
        a = Node(name="A", x=0, y=0)
        r = Node(name="R", x=2500, y=0, role="ROUTER")
        b = Node(name="B", x=5000, y=0)
        scenario = Scenario(nodes=[a, r, b])
        scenario.environment.path_loss_exponent = 3.2
        scenario.environment.stochastic = False
        packet = PacketConfig(source_id=a.id, destination_id="BROADCAST", hop_limit=2)
        result = SimulationEngine(scenario).run(packet)
        self.assertIn(r.id, result.reached)
        self.assertIn(b.id, result.reached)

    def test_acknowledged_dm_learns_and_reuses_directed_path(self):
        source = Node(id="source", name="Source", x=0, y=0)
        relay = Node(id="relay", name="Relay", x=600, y=0, role="ROUTER")
        destination = Node(id="destination", name="Destination", x=1200, y=0)
        scenario = Scenario(nodes=[source, relay, destination])
        scenario.environment.path_loss_exponent = 4.2
        scenario.environment.stochastic = False
        packet = PacketConfig(
            source_id=source.id,
            destination_id=destination.id,
            hop_limit=2,
            want_ack=True,
        )

        discovery = SimulationEngine(scenario).run(packet)
        key = dm_route_key(source.id, destination.id)
        self.assertEqual(discovery.routing_mode, "DM_DISCOVERY_FLOOD")
        self.assertTrue(result_uses_coverage_ripples(discovery))
        self.assertTrue(discovery.acknowledged)
        self.assertEqual(discovery.learned_route, [source.id, relay.id, destination.id])
        self.assertEqual(scenario.learned_routes[key], discovery.learned_route)

        directed = SimulationEngine(scenario).run(packet)
        self.assertEqual(directed.routing_mode, "DM_LEARNED")
        self.assertFalse(result_uses_coverage_ripples(directed))
        self.assertEqual(directed.transmissions, 2)
        self.assertEqual(set(directed.reached), {source.id, relay.id, destination.id})
        self.assertEqual(
            [(event.peer_id, event.node_id) for event in directed.events if event.kind == "RX"],
            [(source.id, relay.id), (relay.id, destination.id)],
        )

    def test_failed_learned_dm_path_is_invalidated_and_falls_back_to_flooding(self):
        source = Node(id="source", x=0, y=0)
        relay = Node(id="relay", x=600, y=0, role="ROUTER")
        destination = Node(id="destination", x=1200, y=0)
        key = dm_route_key(source.id, destination.id)
        scenario = Scenario(
            nodes=[source, relay, destination],
            learned_routes={key: [source.id, relay.id, destination.id]},
        )
        scenario.environment.path_loss_exponent = 4.2
        scenario.environment.stochastic = False
        relay.online = False
        packet = PacketConfig(
            source_id=source.id,
            destination_id=destination.id,
            hop_limit=2,
            want_ack=True,
        )

        result = SimulationEngine(scenario).run(packet)
        self.assertEqual(result.routing_mode, "DM_FALLBACK_FLOOD")
        self.assertTrue(result_uses_coverage_ripples(result))
        self.assertEqual(result.invalidated_route_key, key)
        self.assertNotIn(key, scenario.learned_routes)
        self.assertTrue(any(event.kind == "ROUTE_FALLBACK" for event in result.events))

    def test_learned_dm_routes_roundtrip_with_scenario(self):
        key = dm_route_key("source", "destination")
        restored = Scenario.from_json(
            Scenario(learned_routes={key: ["source", "relay", "destination"]}).to_json()
        )
        self.assertEqual(restored.learned_routes[key], ["source", "relay", "destination"])

    def test_packet_path_reconstructs_entire_first_arrival_route(self):
        result = SimulationResult(
            reached={
                "source": {"hop": 0, "via": ""},
                "router": {"hop": 1, "via": "source"},
                "target": {"hop": 2, "via": "router"},
            }
        )
        self.assertEqual(packet_path_node_ids(result, "target"), ["source", "router", "target"])
        self.assertEqual(packet_path_node_ids(result, "missing"), [])

    def test_unit_formatting_supports_imperial_and_metric(self):
        self.assertEqual(format_distance_value(1609.344, "Imperial"), "1.00 mi")
        self.assertEqual(format_distance_value(100.0, "Imperial"), "328 ft")
        self.assertEqual(format_distance_value(1609.344, "Metric"), "1.61 km")
        self.assertEqual(format_area_value(2_589_988.110336, "Imperial"), "1.00 mi²")
        self.assertEqual(format_area_value(1_000_000.0, "Metric"), "1.00 km²")

    def test_random_nodes_are_spread_across_separate_viewport_regions(self):
        points = spread_random_points(25, 0, 0, 1_500, 800, seed=42)
        self.assertEqual(points, spread_random_points(25, 0, 0, 1_500, 800, seed=42))
        self.assertEqual(len(points), 25)
        self.assertTrue(all(0 <= x <= 1_500 and 0 <= y <= 800 for x, y in points))
        self.assertGreater(max(x for x, _y in points) - min(x for x, _y in points), 1_200)
        self.assertGreater(max(y for _x, y in points) - min(y for _x, y in points), 600)

        available = spread_random_points_in_regions(
            25,
            [(50, 150, 1_450, 760), (370, 35, 1_450, 150)],
            seed=42,
        )
        self.assertEqual(len(available), 25)
        self.assertTrue(all(not (x < 370 and y < 150) for x, y in available))
        self.assertGreater(max(y for _x, y in available) - min(y for _x, y in available), 650)

    def test_coverage_can_be_streamed_by_hop_without_changing_boundaries(self):
        source = Node(id="source", name="Source", x=100, y=500)
        router = Node(id="router", name="Router", x=600, y=500, role="ROUTER")
        target = Node(id="target", name="Target", x=1100, y=500)
        scenario = Scenario(nodes=[source, router, target], obstacles=[])
        scenario.environment.stochastic = False
        packet = PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=2)
        result = SimulationEngine(scenario).run(packet)
        grouped = transmitter_ids_by_hop(result)
        complete = build_coverage_contours(scenario, result)
        streamed: dict[str, list[tuple[float, float, str]]] = {}

        for hop in sorted(grouped):
            streamed.update(
                build_coverage_contours(
                    scenario,
                    result,
                    transmitter_ids=grouped[hop],
                )
            )

        self.assertEqual(streamed, complete)
        self.assertEqual(grouped[min(grouped)], [source.id])

    def test_coverage_contour_stops_at_hard_obstruction(self):
        source = Node(name="Source", x=400, y=500)
        target = Node(name="Target", x=1500, y=500)
        wall = Obstacle(
            name="Blocking wall",
            kind="Wall",
            x1=800,
            y1=0,
            x2=900,
            y2=1000,
            height_m=50,
            behavior="BLOCK",
            attenuation_db=30,
        )
        scenario = Scenario(nodes=[source, target], obstacles=[wall])
        scenario.environment.initial_view_width_m = 2000
        scenario.environment.initial_view_height_m = 1000
        scenario.environment.stochastic = False
        packet = PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=1)
        result = SimulationEngine(scenario).run(packet)
        contour = build_coverage_contours(scenario, result)[source.id]
        east_x, east_y, east_kind = contour[0]
        self.assertEqual(east_kind, "blocked")
        self.assertGreater(east_x, 780)
        self.assertLess(east_x, 810)
        self.assertAlmostEqual(east_y, source.y, delta=2)

    def test_unobstructed_coverage_is_round_and_not_clipped_to_world_rectangle(self):
        source = Node(name="Source", x=1_000, y=500)
        receiver = Node(name="Receiver", x=1_100, y=500)
        scenario = Scenario(nodes=[source, receiver])
        scenario.environment.initial_view_width_m = 2_000
        scenario.environment.initial_view_height_m = 1_000
        scenario.environment.stochastic = False
        result = SimulationEngine(scenario).run(
            PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=1)
        )
        contour = build_coverage_contours(scenario, result)[source.id]
        radii = [math.hypot(x - source.x, y - source.y) for x, y, _kind in contour]
        self.assertGreater(min(radii), math.hypot(2_000, 1_000))
        self.assertLess((max(radii) - min(radii)) / min(radii), 0.002)
        self.assertTrue(all(kind == "threshold" for _x, _y, kind in contour))


    def test_client_mute_does_not_bridge(self):
        scenario = create_demo_scenario()
        source = scenario.nodes[0]
        scenario.nodes[1].role = "CLIENT_MUTE"
        scenario.environment.stochastic = False
        packet = PacketConfig(source_id=source.id, destination_id="BROADCAST", hop_limit=3)
        result = SimulationEngine(scenario).run(packet)
        no_relay = [event for event in result.events if event.kind == "NO_RELAY" and event.node_id == scenario.nodes[1].id]
        self.assertTrue(no_relay or scenario.nodes[1].id not in result.reached)

    def test_roundtrip_json(self):
        scenario = create_demo_scenario()
        restored = Scenario.from_json(scenario.to_json())
        self.assertEqual(len(restored.nodes), len(scenario.nodes))
        self.assertEqual(restored.nodes[1].radio.spreading_factor, scenario.nodes[1].radio.spreading_factor)
        self.assertEqual(restored.obstacles[0].kind, "Mountain")

    def test_link_symmetry_for_equal_radios(self):
        a = Node(x=0, y=0)
        b = Node(x=1000, y=500)
        scenario = Scenario(nodes=[a, b])
        model = PropagationModel(scenario)
        self.assertTrue(math.isclose(model.link(a, b).rssi_dbm, model.link(b, a).rssi_dbm, abs_tol=1e-8))


if __name__ == "__main__":
    unittest.main()
