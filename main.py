import sys
import tkinter as tk

from mesh_simulator.geography import MapDataService
from mesh_simulator.model import SimulationEngine, create_demo_scenario
from mesh_simulator.ui import MeshSimulatorApp, run


def smoke_test() -> None:
    root = tk.Tk()
    root.withdraw()
    app = MeshSimulatorApp(root)
    app.scenario = create_demo_scenario()
    app.refresh_all()
    root.update_idletasks()
    packet = app._read_packet_form()
    if packet is None:
        raise RuntimeError("Default packet configuration is invalid")
    result = SimulationEngine(app.scenario).run(packet)
    if not result.events or not result.reached:
        raise RuntimeError("Simulation produced no events or reached nodes")
    env = app.scenario.environment
    app.survey_measurements = [
        {
            "sequence": 1,
            "epoch_s": 1_700_000_000,
            "mobile_latitude": env.map_center_lat + 0.001,
            "mobile_longitude": env.map_center_lon + 0.001,
            "mobile_satellites": 9,
            "mobile_hdop": 1.1,
            "base_latitude": env.map_center_lat,
            "base_longitude": env.map_center_lon,
            "forward_received": True,
            "forward_rssi_dbm": -101,
            "forward_snr_db": -2.5,
            "reply_received": True,
            "reverse_rssi_dbm": -97,
            "reverse_snr_db": 1.25,
        }
    ]
    app.show_survey_viewer()
    root.update_idletasks()
    if len(app.survey_tree.get_children()) != 1:
        raise RuntimeError("Survey viewer did not display its measurement")
    if not app.canvas.find_withtag("survey-layer"):
        raise RuntimeError("Survey measurement was not plotted on the map")
    app._close_survey_viewer()
    app.stop_animation()
    root.destroy()


def overture_smoke_test() -> None:
    buildings = MapDataService().fetch_overture_buildings(
        40.903,
        -74.211,
        40.905,
        -74.209,
    )
    if not buildings:
        raise RuntimeError("Overture returned no buildings for the diagnostic area")
    if not all(element.get("type") == "overture" for element in buildings):
        raise RuntimeError("Overture returned an unexpected feature format")


if __name__ == "__main__":
    if "--overture-smoke-test" in sys.argv:
        overture_smoke_test()
    elif "--smoke-test" in sys.argv:
        smoke_test()
    else:
        run()
