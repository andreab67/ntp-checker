"""PostgreSQL integration checks for the dashboard and monitor database paths."""
from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import unittest
from unittest import mock

import psycopg

os.environ.setdefault("LOG_PATH", "/tmp/ntp-checker-test.log")

import app
import monitor

TEST_DATABASE_URL = os.environ.get("NTP_TEST_DATABASE_URL")
TEST_TABLE = "metrics.ntp_parent"


def require_local_test_database(dsn: str) -> dict[str, str]:
    connection_info = psycopg.conninfo.conninfo_to_dict(dsn)
    hostaddr = connection_info.get("hostaddr")
    try:
        local_hostaddrs = not hostaddr or all(
            ipaddress.ip_address(address.strip()).is_loopback
            for address in hostaddr.split(",")
        )
    except ValueError:
        local_hostaddrs = False

    if (
        connection_info.get("dbname") != "ntp_checker_test"
        or connection_info.get("host") not in {"127.0.0.1", "localhost"}
        or not local_hostaddrs
    ):
        raise RuntimeError("database integration tests require a local ntp_checker_test database")
    return connection_info


class DatabaseDsnGuardTests(unittest.TestCase):
    def test_rejects_wrong_database_and_remote_hostaddr_before_connect(self) -> None:
        unsafe_dsns = (
            "postgresql://postgres:postgres@127.0.0.1:5432/ntp_checker_other",
            "postgresql://postgres:postgres@127.0.0.1:5432/ntp_checker_test?hostaddr=192.0.2.10",
        )
        test_module = sys.modules[__name__]
        for dsn in unsafe_dsns:
            with (
                self.subTest(dsn=dsn),
                mock.patch.object(test_module, "TEST_DATABASE_URL", dsn),
                mock.patch.object(psycopg, "connect") as connect,
                self.assertRaises(RuntimeError),
            ):
                DatabaseDriverTests.setUpClass()
                connect.assert_not_called()


@unittest.skipUnless(TEST_DATABASE_URL, "NTP_TEST_DATABASE_URL must name the disposable test database")
class DatabaseDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_local_test_database(TEST_DATABASE_URL)

        with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS metrics")
            cur.execute("DROP TABLE IF EXISTS metrics.ntp_parent")
            cur.execute(
                """
                CREATE TABLE metrics.ntp_parent (
                    ts timestamptz PRIMARY KEY,
                    last_offset_sec double precision,
                    stratum integer,
                    total_sources integer,
                    leap_status text,
                    gps_mode text,
                    selected_source text,
                    gps_summary text
                )
                """
            )

    def setUp(self) -> None:
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            conn.execute("TRUNCATE metrics.ntp_parent")
        for module, name, value in (
            (app, "DB_URL", TEST_DATABASE_URL),
            (monitor, "DB_URL", TEST_DATABASE_URL),
            (monitor, "POSTGRES_TABLE", TEST_TABLE),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dashboard_reads_named_rows_with_psycopg3(self) -> None:
        with mock.patch.object(psycopg, "connect", wraps=psycopg.connect) as connect:
            rows = app.q(
                "SELECT %s::text AS station, %s::integer AS stratum",
                ("clock.example.test", 3),
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["station"], "clock.example.test")
        self.assertEqual(rows[0]["stratum"], 3)
        connect.assert_called_once()

    def test_monitor_inserts_a_sample_with_psycopg3(self) -> None:
        tracking = {"last_offset_sec": 0.002, "stratum": 3, "leap_status": "Normal"}
        sources = {"total_sources": 7, "selected_line": "* 192.0.2.1"}

        with mock.patch.object(psycopg, "connect", wraps=psycopg.connect) as connect:
            monitor.db_insert_sample(tracking, sources, True, "3D fix")

        connect.assert_called_once()
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            row = conn.execute(
                """
                SELECT last_offset_sec, stratum, total_sources, leap_status,
                       gps_mode, selected_source, gps_summary
                FROM metrics.ntp_parent
                """
            ).fetchone()

        self.assertEqual(
            row,
            (0.002, 3, 7, "Normal", "3D fix", "* 192.0.2.1", "3D fix"),
        )


    def test_parsed_sample_flows_through_dashboard_apis(self) -> None:
        tracking = monitor.parse_tracking(
            "Leap status     : Normal\nStratum         : 3\n"
            "Last offset     : 0.000000 seconds"
        )
        sources = monitor.parse_sources(
            "MS Name/IP address         Stratum Poll Reach LastRx Last sample\n"
            "^* 192.0.2.1                    2   6   377    31   +0ns[+0ns] +/- 1ms\n"
            "^+ 192.0.2.2                    2   6   377    30   +1us[+1us] +/- 2ms"
        )
        gps_output = (
            '{"class":"TPV","mode":3,"time":"2026-11-03T12:00:00Z",'
            '"lat":40.0,"lon":-105.0}'
        )
        with mock.patch.object(
            monitor.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["ssh"], returncode=0, stdout=gps_output, stderr=""
            ),
        ) as run:
            gps_ok, gps_summary = monitor.run_gps_status()
        run.assert_called_once()

        self.assertEqual(tracking, {
            "leap_status": "Normal", "stratum": 3, "last_offset_sec": 0.0
        })
        self.assertTrue(sources["has_selected"])
        self.assertEqual(sources["total_sources"], 2)
        self.assertTrue(gps_ok)
        self.assertIn("mode=3D fix", gps_summary)
        self.assertIn("pos=(40.0,-105.0)", gps_summary)
        monitor.db_insert_sample(tracking, sources, gps_ok, gps_summary)

        client = app.app.test_client()
        latest = client.get("/api/latest")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.get_json()["stratum"], 3)
        self.assertEqual(latest.get_json()["gps_mode"], "3D fix")
        self.assertEqual(latest.get_json()["leap_status"], "Normal")

        offset = client.get("/api/offset?window=24h&interval=5min")
        self.assertEqual(offset.status_code, 200)
        self.assertEqual(len(offset.get_json()), 1)
        self.assertEqual(offset.get_json()[0]["avg"], 0.0)
        self.assertEqual(offset.get_json()[0]["p95"], 0.0)
        self.assertEqual(offset.get_json()[0]["max"], 0.0)

    def test_empty_database_and_invalid_window_responses(self) -> None:
        client = app.app.test_client()
        with mock.patch.object(psycopg, "connect", wraps=psycopg.connect) as connect:
            self.assertEqual(client.get("/api/latest").get_json(), {})
            self.assertEqual(client.get("/api/offset").get_json(), [])
            self.assertEqual(connect.call_count, 2)
            invalid = client.get("/api/offset?window=forever&interval=5min")
            self.assertEqual(connect.call_count, 2)

        self.assertEqual(invalid.status_code, 400)

    def test_query_failure_closes_connection_and_following_query_succeeds(self) -> None:
        real_connect = psycopg.connect
        connections = []

        def capture_connection(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with (
            mock.patch.object(psycopg, "connect", side_effect=capture_connection),
            self.assertRaises(psycopg.errors.UndefinedTable),
        ):
            app.q("SELECT * FROM metrics.table_that_does_not_exist")

        self.assertEqual(len(connections), 1)
        self.assertTrue(connections[0].closed)
        rows = app.q("SELECT 1 AS available")
        self.assertEqual(rows[0]["available"], 1)

    def test_failed_insert_rolls_back_and_next_insert_succeeds(self) -> None:
        tracking = {"last_offset_sec": 0.004, "stratum": 2, "leap_status": "Normal"}
        sources = {"total_sources": 1, "selected_line": "^* 192.0.2.1"}

        real_connect = psycopg.connect
        connections = []

        def capture_connection(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with (
            mock.patch.object(psycopg, "connect", side_effect=capture_connection),
            mock.patch.object(monitor, "POSTGRES_TABLE", "metrics.table_that_does_not_exist"),
        ):
            monitor.db_insert_sample(tracking, sources, True, "3D fix")

        self.assertEqual(len(connections), 1)
        self.assertTrue(connections[0].closed)
        monitor.db_insert_sample(tracking, sources, True, "3D fix")
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            row = conn.execute(
                "SELECT count(*), max(last_offset_sec) FROM metrics.ntp_parent"
            ).fetchone()

        self.assertEqual(row, (1, 0.004))


if __name__ == "__main__":
    unittest.main()
