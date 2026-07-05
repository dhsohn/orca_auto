import tempfile
import time
import unittest
from pathlib import Path

from orca_auto.orca.inp_rewriter import (
    ensure_submission_resource_request,
    prepare_checkpoint_restart_input,
    read_resource_request_from_input,
    rewrite_for_retry,
)

BASE_INP = """! OptTS Freq IRC

%pal
  nprocs 8
end

* xyz 0 1
H 0 0 0
H 0 0 0.74
*
"""


class TestInpRewriter(unittest.TestCase):
    def test_ensure_submission_resource_request_injects_missing_directives(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "rxn.inp"
            inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

            resource_request, actions = ensure_submission_resource_request(
                inp,
                default_max_cores=8,
                default_max_memory_gb=32,
            )
            text = inp.read_text(encoding="utf-8")

        self.assertEqual(resource_request, {"max_cores": 8, "max_memory_gb": 32})
        self.assertEqual(actions, ["pal_nprocs_injected", "maxcore_injected"])
        self.assertIn("%pal", text)
        self.assertIn("nprocs 8", text)
        self.assertIn("%maxcore 4096", text)

    def test_ensure_submission_resource_request_preserves_existing_nprocs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "rxn.inp"
            inp.write_text(
                "! Opt\n%pal\n  nprocs 12\nend\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )

            resource_request, actions = ensure_submission_resource_request(
                inp,
                default_max_cores=8,
                default_max_memory_gb=32,
            )
            text = inp.read_text(encoding="utf-8")

        self.assertEqual(resource_request, {"max_cores": 12, "max_memory_gb": 32})
        self.assertEqual(actions, ["maxcore_injected"])
        self.assertIn("nprocs 12", text)
        self.assertIn("%maxcore 2730", text)

    def test_ensure_submission_resource_request_honors_pal_route_shorthand(self) -> None:
        # "! Opt PAL4" already requests 4 processes via ORCA's route shorthand, so
        # no conflicting %pal nprocs block should be injected and the resource
        # request must reflect 4 cores (not the default_max_cores).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "rxn.inp"
            inp.write_text("! Opt PAL4\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

            resource_request, actions = ensure_submission_resource_request(
                inp,
                default_max_cores=8,
                default_max_memory_gb=32,
            )
            text = inp.read_text(encoding="utf-8")

        self.assertEqual(resource_request["max_cores"], 4)
        self.assertNotIn("pal_nprocs_injected", actions)
        self.assertNotIn("%pal", text)

    def test_read_resource_request_from_input_uses_inp_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "rxn.inp"
            inp.write_text(
                "! Opt\n%pal\n  nprocs 6\nend\n%maxcore 3072\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )

            resource_request = read_resource_request_from_input(inp)

        self.assertEqual(resource_request, {"max_cores": 6, "max_memory_gb": 18})

    def test_prepare_checkpoint_restart_input_keeps_original_input_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.resume.inp"
            src.write_text(BASE_INP, encoding="utf-8")
            original = src.read_text(encoding="utf-8")
            (root / "rxn.gbw").write_bytes(b"checkpoint")
            (root / "rxn.xyz").write_text("2\n\nH 0 0 0\nH 0 0 0.75\n", encoding="utf-8")

            prepared, actions = prepare_checkpoint_restart_input(src, dst, root)
            out = dst.read_text(encoding="utf-8")
            unchanged = src.read_text(encoding="utf-8")

        self.assertEqual(prepared, dst)
        self.assertEqual(unchanged, original)
        self.assertIn("checkpoint_restart_from_rxn.gbw", actions)
        self.assertIn("route_add_moread", actions)
        self.assertIn("moinp_set", actions)
        self.assertIn("geometry_restart_from_rxn.xyz", actions)
        self.assertIn('%moinp "rxn.gbw"', out)
        self.assertIn("* xyzfile 0 1 rxn.xyz", out)

    def test_prepare_checkpoint_restart_falls_back_to_latest_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.resume.inp"
            src.write_text(BASE_INP, encoding="utf-8")
            (root / "rxn.gbw").write_bytes(b"checkpoint")
            (root / "older.xyz").write_text("2\n\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
            time.sleep(0.01)
            (root / "latest_trj.xyz").write_text("2\n\nH 0 0 0\nH 0 0 1.0\n", encoding="utf-8")

            prepared, actions = prepare_checkpoint_restart_input(src, dst, root)
            out = dst.read_text(encoding="utf-8")

        self.assertEqual(prepared, dst)
        self.assertIn("no_previous_xyz_file_found", actions)
        self.assertIn("geometry_restart_from_latest_trj.xyz", actions)
        self.assertIn("* xyzfile 0 1 latest_trj.xyz", out)

    def test_prepare_checkpoint_restart_marks_missing_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.resume.inp"
            src.write_text(BASE_INP, encoding="utf-8")
            (root / "rxn.gbw").write_bytes(b"checkpoint")

            prepared, actions = prepare_checkpoint_restart_input(src, dst, root)

        self.assertEqual(prepared, dst)
        self.assertIn("no_previous_xyz_file_found", actions)
        self.assertIn("no_geometry_file_found", actions)

    def test_rewrite_for_retry_clamps_maxcore_to_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.retry01.inp"
            src.write_text(
                "! Opt\n%pal\n  nprocs 8\nend\n%maxcore 100000\n"
                "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            actions = rewrite_for_retry(
                src, dst, step="no_route_rewrite", max_memory_gb=32, allow_no_effective_change=True
            )
            text = dst.read_text(encoding="utf-8")
        # 32 GB across 8 cores -> 4096 MB per-core ceiling.
        self.assertIn("%maxcore 4096", text)
        self.assertNotIn("%maxcore 100000", text)
        self.assertIn("maxcore_clamped_to_budget", actions)

    def test_rewrite_for_retry_leaves_within_budget_maxcore_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.retry01.inp"
            src.write_text(
                "! Opt\n%pal\n  nprocs 8\nend\n%maxcore 2000\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            actions = rewrite_for_retry(
                src, dst, step="no_route_rewrite", max_memory_gb=32, allow_no_effective_change=True
            )
            text = dst.read_text(encoding="utf-8")
        self.assertIn("%maxcore 2000", text)
        self.assertNotIn("maxcore_clamped_to_budget", actions)

    def test_rewrite_for_retry_raises_when_no_effective_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.retry01.inp"
            src.write_text(BASE_INP, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no_retry_rewrite_available"):
                rewrite_for_retry(src, dst, step="no_route_rewrite")
        self.assertFalse(dst.exists())

    def test_find_block_range_does_not_mutate_lines(self) -> None:
        """find_block_range must not append 'end' to the shared lines list.

        Before the fix, calling find_block_range on an unclosed block would
        append 'end' to lines, corrupting subsequent block lookups. This test
        verifies repeated reads of unclosed blocks do NOT change the line count.
        """
        from orca_auto.orca.input_blocks import find_block_range

        lines = [
            "! OptTS Freq IRC",
            "",
            "%pal",
            "  nprocs 8",
            "",
            "%scf",
            "  MaxIter 125",
            "",
            "* xyz 0 1",
            "H 0 0 0",
            "H 0 0 0.74",
            "*",
        ]
        original_len = len(lines)
        pal_rng = find_block_range(lines, "pal")
        self.assertIsNotNone(pal_rng)
        self.assertEqual(len(lines), original_len)

        # find_block_range for %scf should still return correct unclosed range
        rng = find_block_range(lines, "scf")
        self.assertIsNotNone(rng)
        assert rng is not None
        start, end, needs_close = rng
        self.assertEqual(start, 5)
        self.assertTrue(needs_close)
        self.assertEqual(len(lines), original_len)

    def test_geom_retry_keys_are_inserted_outside_nested_scan_block(self) -> None:
        from orca_auto.orca.input_blocks import set_block_key_value

        lines = [
            "! ScanTS B3LYP def2-SVP Freq",
            "%geom",
            "  MaxIter 200",
            "  Scan",
            "    B 4 20 = 1.86, 3.40, 32",
            "  end",
            "end",
            "* xyzfile 0 1 input.xyz",
        ]

        changed = set_block_key_value(lines, "geom", "Calc_Hess", "true")

        self.assertTrue(changed)
        self.assertEqual(
            lines,
            [
                "! ScanTS B3LYP def2-SVP Freq",
                "%geom",
                "  MaxIter 200",
                "  Scan",
                "    B 4 20 = 1.86, 3.40, 32",
                "  end",
                "  Calc_Hess true",
                "end",
                "* xyzfile 0 1 input.xyz",
            ],
        )
