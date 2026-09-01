import unittest

import mapbox_vector_tile

from mesh_simulator.geography import get_vector_tile_data


class MapDependencyTests(unittest.TestCase):
    def test_generated_map_vector_decoder_is_available(self):
        tile = mapbox_vector_tile.encode({"name": "land", "features": []})
        service = type(
            "MapService", (), {"fetch_vector_tile_bytes": lambda *_args: tile}
        )()

        self.assertIn("land", get_vector_tile_data(service, 12, 1200, 1500))
