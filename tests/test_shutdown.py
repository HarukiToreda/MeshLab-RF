import threading
import unittest

from mesh_simulator.background import DaemonTask, daemon_map_as_completed
from mesh_simulator.ui import MeshSimulatorApp


class ShutdownTests(unittest.TestCase):
    def test_parallel_import_workers_are_daemon_threads(self):
        daemon_flags: list[bool] = []

        def inspect_worker(value: int) -> int:
            daemon_flags.append(threading.current_thread().daemon)
            return value * 2

        completed = dict(
            daemon_map_as_completed(
                inspect_worker,
                range(8),
                max_workers=3,
                name="ShutdownTest",
            )
        )

        self.assertEqual(completed, {value: value * 2 for value in range(8)})
        self.assertTrue(daemon_flags)
        self.assertTrue(all(daemon_flags))

    def test_single_background_task_is_a_daemon(self):
        task = DaemonTask(lambda: threading.current_thread().daemon, name="ShutdownTaskTest")

        self.assertTrue(task.result())

    def test_window_close_cancels_work_without_running_render_stop_helpers(self):
        class RootStub:
            def __init__(self) -> None:
                self.quit_count = 0
                self.destroy_count = 0

            def quit(self) -> None:
                self.quit_count += 1

            def destroy(self) -> None:
                self.destroy_count += 1

        class RadioStub:
            connected = True
            connecting = False

            def __init__(self) -> None:
                self.disconnect_count = 0

            def disconnect(self) -> None:
                self.disconnect_count += 1

        app = MeshSimulatorApp.__new__(MeshSimulatorApp)
        app.root = RootStub()
        app._closing = False
        app.live_mesh_cancel_event = threading.Event()
        app.beacon_cancel = threading.Event()
        app.static_coverage_cancel = threading.Event()
        app.live_mesh_request_id = 1
        app.beacon_request_id = 2
        app.static_coverage_request_id = 3
        app.simulation_request_id = 4
        app.terrain_request_id = 5
        app.survey_playback_active = True
        app.live_radio = RadioStub()
        app.stop_animation = lambda: self.fail("close must not render animation cleanup")
        app.stop_live_mesh = lambda **_kwargs: self.fail("close must not render live-mesh cleanup")

        app.on_close()
        app.on_close()

        self.assertTrue(app.live_mesh_cancel_event.is_set())
        self.assertTrue(app.beacon_cancel.is_set())
        self.assertTrue(app.static_coverage_cancel.is_set())
        self.assertEqual(app.live_mesh_request_id, 2)
        self.assertEqual(app.beacon_request_id, 3)
        self.assertEqual(app.static_coverage_request_id, 4)
        self.assertEqual(app.simulation_request_id, 5)
        self.assertEqual(app.terrain_request_id, 6)
        self.assertFalse(app.survey_playback_active)
        self.assertEqual(app.live_radio.disconnect_count, 1)
        self.assertEqual(app.root.quit_count, 1)
        self.assertEqual(app.root.destroy_count, 1)


if __name__ == "__main__":
    unittest.main()
