import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.out_analyzer import analyze_output
from orca_auto.orca.statuses import AnalyzerStatus


class TestOutAnalyzer(unittest.TestCase):
    def test_status_is_analyzer_status_enum(self) -> None:
        payload = "****ORCA TERMINATED NORMALLY****\n"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertIsInstance(result.status, AnalyzerStatus)
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)

    def test_completed_ts(self) -> None:
        payload = "\n".join(
            [
                "some line -123.45 cm**-1",
                "IRC PATH SUMMARY",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=True, route_line="! OptTS IRC")
            )
        self.assertEqual(result.status, "completed")

    def test_ts_small_file_avoids_full_rescan(self) -> None:
        payload = "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  -120.00 cm**-1",
                "  140.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            with patch(
                "orca_auto.orca.out_analyzer._scan_ts_full_for_imag_count",
                side_effect=AssertionError("full scan called"),
            ):
                result = analyze_output(
                    out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS")
                )
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)

    def test_completed_ts_with_irc_marker_outside_tail_window(self) -> None:
        filler = ("X" * 120 + "\n") * 4000
        payload = "\n".join(
            [
                "IRC PATH SUMMARY",
                filler,
                "some line -123.45 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=True, route_line="! OptTS IRC")
            )
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.markers["irc_marker_found"])

    def test_ts_uses_last_vibrational_frequency_section(self) -> None:
        payload = "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  1   -500.00 cm**-1",
                "  2   -120.00 cm**-1",
                "VIBRATIONAL FREQUENCIES",
                "  1   -150.00 cm**-1",
                "  2    120.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS")
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.markers["imaginary_frequency_count"], 1)

    def test_ts_not_found(self) -> None:
        payload = "\n".join(
            [
                "****ORCA TERMINATED NORMALLY****",
                "TOTAL RUN TIME: 0 days 0 hours 1 minutes 0 seconds",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS")
            )
        self.assertEqual(result.status, "ts_not_found")

    def test_multiplicity_impossible(self) -> None:
        payload = (
            "Error : multiplicity (1) is odd and number of electrons (235) is odd -> impossible"
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "error_multiplicity_impossible")

    def test_scfgrad_abort(self) -> None:
        payload = "ORCA finished by error termination in SCF gradient"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "error_scfgrad_abort")

    def test_scf_not_converged(self) -> None:
        payload = "SCF NOT CONVERGED AFTER 300 CYCLES"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "error_scf")
        self.assertEqual(result.reason, "scf_not_converged")

    def test_disk_io_error(self) -> None:
        payload = "COULD NOT WRITE TO DISK\n"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "error_disk_io")
        self.assertEqual(result.reason, "disk_write_failed")

    def test_zero_distance_geometry_error(self) -> None:
        payload = "\n".join(
            [
                "Calculating Nuclear repulsion ... Error (ORCA_GTOINT/SHARK): ",
                "Zero distance encountered between atoms 61 and 20",
                "ORCA finished by error termination in Startup",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! ScanTS")
            )
        self.assertEqual(result.status, AnalyzerStatus.ERROR_GEOMETRY)
        self.assertEqual(result.reason, "geometry_zero_distance")
        self.assertTrue(result.markers["geometry_zero_distance"])
        self.assertTrue(result.markers["generic_error_termination"])

    def test_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text("", encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.reason, "run_incomplete")

    def test_missing_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nonexistent.out"
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.reason, "output_missing")

    def test_generic_error_termination(self) -> None:
        payload = "ORCA FINISHED BY ERROR TERMINATION\n"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "unknown_failure")
        self.assertEqual(result.reason, "error_termination")

    def test_normal_opt_completed(self) -> None:
        payload = "****ORCA TERMINATED NORMALLY****\nTOTAL RUN TIME: 0 days 0 hours 5 minutes\n"
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reason, "normal_termination")
        self.assertTrue(result.markers["total_run_time_seen"])
