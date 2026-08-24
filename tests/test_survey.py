import binascii
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_simulator.survey import (
    SURVEY_RECORD_MAGIC,
    SURVEY_RECORD_STRUCT,
    SURVEY_RECORD_V1_STRUCT,
    decode_survey_records,
    merge_survey_rows,
    write_rows,
)
from mesh_simulator.survey_device import (
    DeviceCapture,
    DeviceDownload,
    DeviceInfo,
    export_device_pair,
    export_devices,
    read_measurements,
    save_captures,
    validate_device_pair,
)


HEADER = (
    "schema,role,event,session_id,sequence,epoch_s,uptime_ms,node_num,peer_num,local_gps_lock,local_latitude_i,"
    "local_longitude_i,local_altitude_m,local_hdop_centi,local_satellites,remote_gps_lock,remote_latitude_i,"
    "remote_longitude_i,remote_altitude_m,remote_hdop_centi,remote_satellites,local_rx_valid,local_rx_rssi_dbm,"
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
    def test_standalone_binary_record_crc_and_fields_decode(self):
        values = [
            SURVEY_RECORD_MAGIC,
            1,
            1,
            4,
            0x0F,
            42,
            7,
            1_700_000_000,
            1234,
            0x11111111,
            0x22222222,
            401234567,
            -751234567,
            1000,
            125,
            9,
            0,
            402000000,
            -752000000,
            2000,
            150,
            8,
            0,
            -97,
            125,
            -103,
            -175,
            99,
            906875000,
            250,
            11,
            5,
            22,
            bytes(31),
            0,
        ]
        encoded = SURVEY_RECORD_V1_STRUCT.pack(*values)
        values[-1] = binascii.crc32(encoded[:-4]) & 0xFFFFFFFF
        encoded = SURVEY_RECORD_V1_STRUCT.pack(*values)
        rows, invalid = decode_survey_records(encoded)

        self.assertEqual(invalid, 0)
        self.assertEqual(rows[0]["role"], "mobile")
        self.assertEqual(rows[0]["event"], "REPLY_RX")
        self.assertEqual(rows[0]["local_rx_rssi_dbm"], "-97")
        self.assertEqual(rows[0]["remote_rx_snr_centi_db"], "-175")

        damaged = bytearray(encoded)
        damaged[40] ^= 0x01
        rows, invalid = decode_survey_records(bytes(damaged))
        self.assertEqual(rows, [])
        self.assertEqual(invalid, 1)

    def test_compact_v2_record_crc_and_radio_metadata_decode(self):
        values = [
            SURVEY_RECORD_MAGIC,
            2,
            2,
            2,
            0x15,
            77,
            8,
            1_700_000_000,
            12_000,
            0x12345678,
            0x87654321,
            401234567,
            -731234567,
            1234,
            95,
            11,
            9,
            401200000,
            -731200000,
            1500,
            110,
            -101,
            275,
            -99,
            -125,
            0xABCDEF01,
            0,
            0,
        ]
        encoded = SURVEY_RECORD_STRUCT.pack(*values)
        values[-1] = binascii.crc32(encoded[:-4]) & 0xFFFFFFFF
        encoded = SURVEY_RECORD_STRUCT.pack(*values)
        rows, invalid = decode_survey_records(encoded)

        self.assertEqual(invalid, 0)
        self.assertEqual(rows[0]["schema"], "2")
        self.assertEqual(rows[0]["node_num"], str(0x12345678))
        self.assertEqual(rows[0]["reply_sent"], "1")
        self.assertEqual(rows[0]["frequency_hz"], "906875000")

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

    def test_base_only_log_plots_forward_reading_and_remote_mobile_gps(self):
        received = row("base", "PROBE_RX", 0x22222222, 0x11111111, -101, -250)
        received["reply_sent"] = "1"
        measurement = merge_survey_rows([received])[0]

        self.assertEqual(measurement["mobile_node_num"], 0x11111111)
        self.assertAlmostEqual(measurement["mobile_latitude"], 40.2)
        self.assertAlmostEqual(measurement["mobile_longitude"], -75.2)
        self.assertEqual(measurement["forward_rssi_dbm"], -101)
        self.assertEqual(measurement["forward_snr_db"], -2.5)
        self.assertTrue(measurement["forward_received"])
        self.assertTrue(measurement["base_reply_sent"])
        self.assertIsNone(measurement["reply_received"])

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

    def test_compact_mobile_completion_row_replaces_separate_send_row(self):
        reply = row("mobile", "REPLY_RX", 0x11111111, 0x22222222, -97, 125)
        reply["schema"] = "2"
        reply["remote_rx_valid"] = "1"
        reply["remote_rx_rssi_dbm"] = "-103"
        reply["remote_rx_snr_centi_db"] = "-175"
        received = row("base", "PROBE_RX", 0x22222222, 0x11111111, -103, -175)
        received["schema"] = "2"
        received["reply_sent"] = "1"

        measurement = merge_survey_rows([reply, received])[0]

        self.assertEqual(measurement["mobile_node_num"], 0x11111111)
        self.assertTrue(measurement["forward_received"])
        self.assertTrue(measurement["base_reply_sent"])
        self.assertTrue(measurement["reply_received"])

    def test_device_pair_requires_exactly_one_mobile_and_one_base(self):
        mobile = DeviceInfo("COM7", 2, "mobile", 1, 4, 80, {})
        base = DeviceInfo("COM8", 2, "base", 2, 4, 80, {})

        self.assertEqual(validate_device_pair((base, mobile)), (mobile, base))
        with self.assertRaisesRegex(RuntimeError, "one mobile and one base"):
            validate_device_pair((mobile, mobile))
        with self.assertRaisesRegex(RuntimeError, "exactly two"):
            validate_device_pair((mobile,))

    def test_paired_export_writes_reloadable_measurements(self):
        mobile = DeviceInfo("COM7", 2, "mobile", 0x11111111, 1, 80, {})
        base = DeviceInfo("COM8", 2, "base", 0x22222222, 1, 80, {})
        mobile_reply = row("mobile", "REPLY_RX", mobile.node_id, base.node_id, -97, 125)
        mobile_reply["schema"] = "2"
        mobile_reply["remote_rx_valid"] = "1"
        mobile_reply["remote_rx_rssi_dbm"] = "-103"
        mobile_reply["remote_rx_snr_centi_db"] = "-175"
        base_receive = row("base", "PROBE_RX", base.node_id, mobile.node_id, -103, -175)
        base_receive["schema"] = "2"
        base_receive["reply_sent"] = "1"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "survey"

            def fake_download(info, output, progress=None):
                rows = [mobile_reply] if info.role == "mobile" else [base_receive]
                return DeviceDownload(
                    info,
                    output / f"{info.role}.bin",
                    output / f"{info.role}.csv",
                    len(rows),
                    0,
                    rows,
                )

            with patch("mesh_simulator.survey_device.download_device", side_effect=fake_download):
                result = export_device_pair((base, mobile), destination)

            self.assertTrue(result.combined_path.is_file())
            self.assertTrue(result.measurements_path.is_file())
            loaded = read_measurements(result.measurements_path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(float(loaded[0]["forward_rssi_dbm"]), -103.0)
            self.assertEqual(float(loaded[0]["reverse_rssi_dbm"]), -97.0)

    def test_nodes_can_be_exported_sequentially_into_one_survey(self):
        mobile = DeviceInfo("COM177", 2, "mobile", 0x11111111, 1, 80, {})
        base = DeviceInfo("COM178", 2, "base", 0x22222222, 1, 80, {})
        mobile_reply = row("mobile", "REPLY_RX", mobile.node_id, base.node_id, -97, 125)
        mobile_reply["schema"] = "2"
        mobile_reply["remote_rx_valid"] = "1"
        mobile_reply["remote_rx_rssi_dbm"] = "-103"
        mobile_reply["remote_rx_snr_centi_db"] = "-175"
        base_receive = row("base", "PROBE_RX", base.node_id, mobile.node_id, -103, -175)
        base_receive["schema"] = "2"
        base_receive["reply_sent"] = "1"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "survey"

            def fake_download(info, output, progress=None):
                rows = [mobile_reply] if info.role == "mobile" else [base_receive]
                csv_path = output / f"{info.role}-node-{info.node_id:016x}.csv"
                write_rows(csv_path, rows)
                return DeviceDownload(
                    info, output / f"{info.role}.bin", csv_path, len(rows), 0, rows
                )

            with patch("mesh_simulator.survey_device.download_device", side_effect=fake_download):
                first = export_devices((mobile,), destination)
                second = export_devices((base,), destination)

            self.assertEqual(first.roles, ("mobile",))
            self.assertEqual(set(second.roles), {"mobile", "base"})
            self.assertEqual(len(second.measurements), 1)
            self.assertTrue(second.measurements[0]["forward_received"])
            self.assertTrue(second.measurements[0]["reply_received"])

    def test_in_memory_captures_merge_without_discarding_first_node(self):
        mobile = DeviceInfo("COM177", 2, "mobile", 0x11111111, 1, 80, {})
        base = DeviceInfo("COM178", 2, "base", 0x22222222, 1, 80, {})
        mobile_reply = row("mobile", "REPLY_RX", mobile.node_id, base.node_id, -97, 125)
        mobile_reply["remote_rx_valid"] = "1"
        mobile_reply["remote_rx_rssi_dbm"] = "-103"
        mobile_reply["remote_rx_snr_centi_db"] = "-175"
        base_receive = row("base", "PROBE_RX", base.node_id, mobile.node_id, -103, -175)
        base_receive["reply_sent"] = "1"
        captures = {
            "mobile": DeviceCapture(mobile, b"mobile", 1, 0, [mobile_reply]),
        }

        mobile_only = merge_survey_rows(
            record for capture in captures.values() for record in capture.rows
        )
        captures["base"] = DeviceCapture(base, b"base", 1, 0, [base_receive])
        combined = merge_survey_rows(
            record for capture in captures.values() for record in capture.rows
        )

        self.assertEqual(len(mobile_only), 1)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["forward_rssi_dbm"], -103)
        self.assertEqual(combined[0]["reverse_rssi_dbm"], -97)
        with tempfile.TemporaryDirectory() as temporary:
            saved = save_captures(captures.values(), temporary)
            self.assertEqual(set(saved.roles), {"mobile", "base"})
            self.assertTrue(saved.combined_path.is_file())


if __name__ == "__main__":
    unittest.main()
