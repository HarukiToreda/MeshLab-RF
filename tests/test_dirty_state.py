import unittest

from mesh_simulator.model import Scenario
from mesh_simulator.ui import MeshSimulatorApp


class DirtyStateTests(unittest.TestCase):
    @staticmethod
    def _topography_app(*, mark_dirty: bool) -> tuple[MeshSimulatorApp, list[bool]]:
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(name="Untitled scenario")
        app.scenario.environment.map_configured = True
        app.scenario.environment.map_center_lat = 0.0
        app.scenario.environment.map_center_lon = 0.0
        app.terrain_request_id = 1
        app.terrain_request_marks_dirty = mark_dirty
        app.pending_terrain_rf_refresh = None
        app.beacon_node_id = None
        app.terrain_visual_key = None
        app.terrain_visual_source = None
        app.status_var = type("Status", (), {"set": lambda _self, _value: None})()
        app._live_mesh_running = lambda: False
        app._standalone_packet_active = lambda: False
        app._mark_results_stale = lambda: None
        app.refresh_all = lambda: None
        app.format_distance = lambda value: str(value)
        app._refresh_active_rf_after_scene_change = lambda **_kwargs: None
        dirty_calls: list[bool] = []
        app.mark_dirty = lambda: dirty_calls.append(True)
        return app, dirty_calls

    def test_startup_terrain_does_not_mark_blank_scenario_dirty(self):
        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.scenario = Scenario(name="Untitled scenario")
        app.scenario.environment.map_configured = True
        app.simulation_thread = None
        calls: list[bool] = []
        app.load_topography = lambda *, mark_dirty=True: calls.append(mark_dirty)

        app._load_startup_terrain()

        self.assertEqual(calls, [False])

    def test_startup_topography_result_keeps_session_clean(self):
        app, dirty_calls = self._topography_app(mark_dirty=False)

        app._apply_topography(
            (1, (0.0, 0.0, -100.0, -100.0, 100.0, 100.0, 0.0, 0.0), (2, 2, [0.0] * 4, 1))
        )

        self.assertEqual(dirty_calls, [])

    def test_manual_topography_result_marks_session_dirty(self):
        app, dirty_calls = self._topography_app(mark_dirty=True)

        app._apply_topography(
            (1, (0.0, 0.0, -100.0, -100.0, 100.0, 100.0, 0.0, 0.0), (2, 2, [0.0] * 4, 1))
        )

        self.assertEqual(dirty_calls, [True])


if __name__ == "__main__":
    unittest.main()
