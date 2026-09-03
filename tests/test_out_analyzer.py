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

    def test_ts_frequency_section_before_the_final_energy_verifies_nothing(self) -> None:
        # OptTS with Calc_Hess but without Freq: the only frequency section is
        # the initial Hessian's, printed before the optimization ran.
        payload = "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  1   -650.00 cm**-1",
                "  2    120.00 cm**-1",
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                "                    ***        THE OPTIMIZATION HAS CONVERGED      ***",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS")
            )
        self.assertEqual(result.status, AnalyzerStatus.TS_NOT_FOUND)
        self.assertEqual(result.reason, "ts_criteria_failed")
        self.assertEqual(result.markers["imaginary_frequency_count"], 0)
        self.assertFalse(result.markers["final_frequency_section"])

    def test_ts_counts_only_the_frequency_section_after_the_last_final_energy(self) -> None:
        # OptTS Freq with Recalc_Hess: recalculated Hessians print sections
        # mid-optimization; the final Freq follows the last final energy.
        payload = "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  1   -650.00 cm**-1",
                "  2   -120.00 cm**-1",
                "FINAL SINGLE POINT ENERGY      -100.100000000000",
                "VIBRATIONAL FREQUENCIES",
                "  1   -600.00 cm**-1",
                "  2   -110.00 cm**-1",
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "VIBRATIONAL FREQUENCIES",
                "  1   -420.00 cm**-1",
                "  2    120.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS Freq")
            )
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)
        self.assertEqual(result.reason, "ts_criteria_met")
        self.assertEqual(result.markers["imaginary_frequency_count"], 1)
        self.assertTrue(result.markers["final_frequency_section"])

    def test_ts_rejected_for_two_modes_keeps_the_final_section_count(self) -> None:
        payload = "\n".join(
            [
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "VIBRATIONAL FREQUENCIES",
                "  1   -420.00 cm**-1",
                "  2   -120.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS Freq")
            )
        self.assertEqual(result.status, AnalyzerStatus.TS_NOT_FOUND)
        self.assertEqual(result.markers["imaginary_frequency_count"], 2)
        self.assertTrue(result.markers["final_frequency_section"])

    def test_ts_final_section_is_not_a_verdict_when_the_run_failed(self) -> None:
        # ORCA can continue to the Freq step after MaxIter and terminate
        # normally; the section then describes a non-stationary geometry.
        for failure_line, expected_status in (
            ("THE OPTIMIZATION DID NOT CONVERGE", AnalyzerStatus.GEOM_NOT_CONVERGED),
            ("SCF NOT CONVERGED", AnalyzerStatus.ERROR_SCF),
        ):
            payload = "\n".join(
                [
                    failure_line,
                    "FINAL SINGLE POINT ENERGY      -100.200000000000",
                    "VIBRATIONAL FREQUENCIES",
                    "  1   -420.00 cm**-1",
                    "****ORCA TERMINATED NORMALLY****",
                ]
            )
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "a.out"
                out.write_text(payload, encoding="utf-8")
                result = analyze_output(
                    out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS Freq")
                )
            self.assertEqual(result.status, expected_status)
            self.assertEqual(result.markers["imaginary_frequency_count"], 1)
            self.assertFalse(result.markers["final_frequency_section"])

    def test_ts_legacy_headerless_count_is_not_a_final_section(self) -> None:
        payload = "\n".join(
            [
                "FINAL SINGLE POINT ENERGY      -100.200000000000",
                "some line   -420.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS Freq")
            )
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)
        self.assertEqual(result.markers["imaginary_frequency_count"], 1)
        self.assertFalse(result.markers["final_frequency_section"])

    def test_ts_ignores_tiny_negative_modes(self) -> None:
        payload = "\n".join(
            [
                "VIBRATIONAL FREQUENCIES",
                "  1     -5.00 cm**-1",
                "  2   -450.00 cm**-1",
                "  3    120.00 cm**-1",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="ts", require_irc=False, route_line="! OptTS")
            )
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)
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

    def test_normal_terminated_unconverged_opt_is_not_completed(self) -> None:
        payload = "\n".join(
            [
                "THE OPTIMIZATION DID NOT CONVERGE",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, AnalyzerStatus.GEOM_NOT_CONVERGED)
        self.assertEqual(result.reason, "geometry_not_converged")

    def test_normal_terminated_later_converged_opt_overrides_earlier_warning(self) -> None:
        payload = "\n".join(
            [
                "OPTIMIZATION HAS NOT YET CONVERGED",
                "THE OPTIMIZATION HAS CONVERGED",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, AnalyzerStatus.COMPLETED)
        self.assertEqual(result.reason, "normal_termination")

    def test_normal_terminated_later_unconverged_opt_overrides_earlier_convergence(self) -> None:
        payload = "\n".join(
            [
                "THE OPTIMIZATION HAS CONVERGED",
                "THE OPTIMIZATION DID NOT CONVERGE",
                "****ORCA TERMINATED NORMALLY****",
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.out"
            out.write_text(payload, encoding="utf-8")
            result = analyze_output(
                out, CompletionMode(kind="opt", require_irc=False, route_line="! Opt")
            )
        self.assertEqual(result.status, AnalyzerStatus.GEOM_NOT_CONVERGED)
        self.assertEqual(result.reason, "geometry_not_converged")


def test_not_converged_marker_before_the_tail_window_is_still_a_verdict(tmp_path):
    # The parser scans the whole file; the analyzer used to read only the last
    # 64 KiB, so a not-converged marker followed by a long normal-modes matrix
    # and a normal termination was reported COMPLETED.
    from orca_auto.orca import out_analyzer

    out_path = tmp_path / "rxn.out"
    filler = "\n".join(f"{i:6d}   0.000000   0.000000   0.000000" for i in range(4000))
    out_path.write_text(
        "! Opt Freq B3LYP def2-SVP\n"
        "THE OPTIMIZATION DID NOT CONVERGE\n"
        "NORMAL MODES\n" + filler + "\n"
        "****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )
    assert out_path.stat().st_size > out_analyzer._DEFAULT_TAIL_BYTES

    analysis = analyze_output(
        out_path, CompletionMode(kind="opt", require_irc=False, route_line="! Opt Freq")
    )

    assert analysis.status is AnalyzerStatus.GEOM_NOT_CONVERGED
    assert analysis.markers["last_opt_converged"] is False
