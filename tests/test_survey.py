import csv
import io
import unittest

from mesh_simulator.survey import merge_survey_rows


HEADER = (
    "schema,role,event,session_id,sequence,epoch_s,uptime_ms,node_num,peer_num,local_gps_lock,local_latitude_i,"
    "local_longitude_i,local_altitude_m,local_pdop_centi,local_satellites,remote_gps_lock,remote_latitude_i,"
    "remote_longitude_i,remote_altitude_m,remote_pdop_centi,remote_satellites,local_rx_valid,local_rx_rssi_dbm,"
    "local_rx_snr_centi_db,remote_rx_valid,remote_rx_rssi_dbm,remote_rx_snr_centi_db,packet_id,"
    "channel_utilization_centi_pct,tx_utilization_centi_pct,region,modem_preset,frequency_hz,tx_power_dbm"
)


def row(role: str, event: str, node: int, peer: int, local_rssi: int = 0, local_snr: int = 0) -> dict[str, str]:
    values = [
        1,
        role,
        event,
        42,
        7,
        1_700_000_000,
        1234,
        node,
        peer,
        1,
        401234567,
        -751234567,
        10,
        125,
        9,
        1,
        402000000,
        -752000000,
        20,
        150,
        8,
        int(bool(local_rssi)),
        local_rssi,
        local_snr,
        0,
        0,
        0,
        99,
        100,
        50,
        1,
        0,
        906875000,
        30,
    ]
    return next(csv.DictReader(io.StringIO(HEADER + "\n" + ",".join(map(str, values)) + "\n")))


class SurveyMergeTests(unittest.TestCase):
    def test_base_boot_row_identifies_total_forward_packet_loss(self):
        sent = row("mobile", "SEND", 0x11111111, 0)
        base_boot = row("base", "BOOT", 0x22222222, 0)
        base_boot["sequence"] = "0"
        measurement = merge_survey_rows([sent, base_boot])[0]

        self.assertEqual(measurement["base_node_num"], 0x22222222)
        self.assertFalse(measurement["forward_received"])
        self.assertFalse(measurement["reply_received"])
        self.assertIsNone(measurement["forward_rssi_dbm"])

    def test_both_device_logs_distinguish_forward_and_reverse_loss(self):
        sent = row("mobile", "SEND", 0x11111111, 0)
        received = row("base", "PROBE_RX", 0x22222222, 0x11111111, -101, -250)
        measurement = merge_survey_rows([sent, received])[0]

        self.assertTrue(measurement["forward_received"])
        self.assertFalse(measurement["reply_received"])
        self.assertEqual(measurement["forward_rssi_dbm"], -101)
        self.assertEqual(measurement["forward_snr_db"], -2.5)
        self.assertIsNone(measurement["reverse_rssi_dbm"])

    def test_mobile_reply_contains_both_link_directions(self):
        sent = row("mobile", "SEND", 0x11111111, 0)
        reply = row("mobile", "REPLY_RX", 0x11111111, 0x22222222, -97, 125)
        reply["remote_rx_valid"] = "1"
        reply["remote_rx_rssi_dbm"] = "-103"
        reply["remote_rx_snr_centi_db"] = "-175"
        measurement = merge_survey_rows([sent, reply])[0]

        self.assertTrue(measurement["forward_received"])
        self.assertTrue(measurement["reply_received"])
        self.assertEqual(measurement["forward_rssi_dbm"], -103)
        self.assertEqual(measurement["forward_snr_db"], -1.75)
        self.assertEqual(measurement["reverse_rssi_dbm"], -97)
        self.assertEqual(measurement["reverse_snr_db"], 1.25)


if __name__ == "__main__":
    unittest.main()
