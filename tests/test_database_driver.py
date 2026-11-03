"""PostgreSQL integration checks for the dashboard and monitor database paths."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import psycopg
from psycopg.rows import dict_row

os.environ.setdefault("LOG_PATH", "/tmp/ntp-checker-test.log")

import app
import monitor

TEST_DATABASE_URL = os.environ.get("NTP_TEST_DATABASE_URL")
TEST_TABLE = "metrics.ntp_parent"


@unittest.skipUnless(TEST_DATABASE_URL, "NTP_TEST_DATABASE_URL must name the disposable test database")
class DatabaseDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        connection_info = psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL)
        if (
            connection_info.get("dbname") != "ntp_checker_test"
            or connection_info.get("host") not in {"127.0.0.1", "localhost"}
        ):
            raise RuntimeError("database integration tests require a local ntp_checker_test database")

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
        app.DB_URL = TEST_DATABASE_URL
        monitor.DB_URL = TEST_DATABASE_URL
        monitor.POSTGRES_TABLE = TEST_TABLE

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
        self.assertIs(connect.call_args.kwargs.get("row_factory"), dict_row)

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


if __name__ == "__main__":
    unittest.main()
