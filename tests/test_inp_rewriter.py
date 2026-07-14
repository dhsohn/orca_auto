import tempfile
import time
import unittest
from pathlib import Path

from orca_auto.orca.inp_rewriter import (
    ensure_submission_resource_request,
    prepare_checkpoint_restart_input,
    prepare_submission_resource_request,
    read_resource_request_from_input,
    rewrite_for_retry,
)
from orca_auto.orca.input_blocks import ensure_route_keywords, set_block_key_value, set_moinp
from orca_auto.orca.resource_directives import clamp_maxcore_to_budget, read_maxcore, read_nprocs

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
    def test_prepare_submission_resource_request_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "rxn.inp"
            payload = b"! Opt\n%pal nprocs 2 end\n%maxcore 1024\n\xff\n"
            inp.write_bytes(payload)

            with self.assertRaisesRegex(ValueError, "UTF-8"):
                prepare_submission_resource_request(
                    inp,
                    default_max_cores=2,
                    default_max_memory_gb=2,
                )

            self.assertEqual(inp.read_bytes(), payload)

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
                "! Opt\n%pal\n  nprocs 8\nend\n# hidden # %maxcore 100000\n"
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

    def test_mutators_replace_active_directives_after_closed_comments(self) -> None:
        lines = [
            "# Freq is commentary # ! SP",
            "# block provenance # %pal",
            "# stale value # nprocs 999",
            "end",
            '# old checkpoint # %moinp "old.gbw"',
            '%moinp "older.gbw"',
            "* xyz 0 1",
            "H 0 0 0",
            "*",
        ]

        self.assertTrue(ensure_route_keywords(lines, ["Freq"]))
        self.assertTrue(set_block_key_value(lines, "pal", "nprocs", "4"))
        self.assertTrue(set_moinp(lines, Path("new.gbw"), Path.cwd()))

        self.assertEqual(lines[0], "! SP Freq")
        self.assertEqual(read_nprocs(lines), 4)
        self.assertEqual(sum(line.startswith("%pal") for line in lines), 1)
        self.assertIn('%moinp "new.gbw"', lines)
        self.assertEqual(sum(line.startswith("%moinp") for line in lines), 1)
        self.assertFalse(any("999" in line or "old" in line for line in lines))

        inline_lines = ["# hidden # %pal nprocs 999 end", "* xyz 0 1", "H 0 0 0", "*"]
        self.assertTrue(set_block_key_value(inline_lines, "pal", "nprocs", "4"))
        self.assertEqual(inline_lines[0], "%pal nprocs 4 end")
        self.assertEqual(read_nprocs(inline_lines), 4)

    def test_resource_readers_use_maximum_and_maxcore_clamp_collapses_duplicates(self) -> None:
        lines = [
            "%maxcore 1000",
            "# hidden # %maxcore 999999",
            "! SP PAL4 PAL8",
            "* xyz 0 1",
            "H 0 0 0",
            "*",
        ]

        self.assertEqual(read_maxcore(lines), 999999)
        self.assertEqual(read_nprocs(lines), 8)
        self.assertTrue(clamp_maxcore_to_budget(lines, max_memory_gb=4))
        self.assertEqual(read_maxcore(lines), 512)
        self.assertEqual(sum(line.startswith("%maxcore") for line in lines), 1)

    def test_block_mutator_rejects_duplicate_blocks_and_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate %pal blocks"):
            set_block_key_value(
                ["%pal nprocs 4 end", "# hidden # %pal nprocs 999 end"],
                "pal",
                "nprocs",
                "4",
            )
        with self.assertRaisesRegex(ValueError, "duplicate nprocs"):
            duplicate_inline = ["# hidden # %pal nprocs 4 nprocs 999 end"]
            self.assertEqual(read_nprocs(duplicate_inline), 999)
            set_block_key_value(
                duplicate_inline,
                "pal",
                "nprocs",
                "4",
            )

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


SCANTS_NO_GEOMETRY_INP = """! ScanTS B3LYP def2-SVP

%geom
  Scan
    B 0 1 = 1.0, 2.0, 5
  end
end
"""


class TestScantsResumeAllOrNothing(unittest.TestCase):
    def test_scants_resume_never_leaks_a_partial_optts_conversion(self) -> None:
        # The resume path finds a TS guess (refinement marker + same-stem xyz)
        # but the malformed input has no geometry block to replace. The OptTS
        # conversion cannot be completed, so none of it may leak into the
        # written input: previously the route was already flipped to OPTTS and
        # the scan block removed before the geometry check bailed.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "rxn.inp"
            dst = root / "rxn.resume.inp"
            src.write_text(SCANTS_NO_GEOMETRY_INP, encoding="utf-8")
            (root / "rxn.gbw").write_bytes(b"checkpoint")
            (root / "rxn.out").write_text("REFINING THE TS GUESS STRUCTURE\n", encoding="utf-8")
            (root / "rxn.xyz").write_text("2\n\nH 0 0 0\nH 0 0 0.75\n", encoding="utf-8")

            prepared, actions = prepare_checkpoint_restart_input(src, dst, root)
            out = dst.read_text(encoding="utf-8")

        self.assertEqual(prepared, dst)
        self.assertNotIn("scants_resume_to_optts", actions)
        self.assertNotIn("scants_scan_block_removed", actions)
        self.assertNotIn("OPTTS", out)
        self.assertIn("ScanTS", out)
        self.assertIn("B 0 1 = 1.0, 2.0, 5", out)
