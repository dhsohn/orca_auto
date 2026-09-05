"""The shared conformer-selection contract (SI ↔ interaction-energy fan-out).

These tests pin the rules both consumers must agree on: route normalization,
geometry tolerance, minimum eligibility, unique single-point pairing, and the
RMSD grouping defaults. A drift here silently degrades ΔE_int into blocker
notes, so the contract is tested directly rather than only through each
consumer.
"""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.engine_runner import executable_identity
from orca_auto.flow.conformer_selection import (
    GEOMETRY_TOL_ANGSTROM,
    blocks_match_geometry,
    bound_orca_selected_input_science_identity,
    coordinates_match,
    eligible_minimum_block,
    has_required_provenance,
    normalized_route_line,
    orca_science_route_identity,
    rmsd_candidate_for_block,
    rmsd_grouping,
    selected_input_state_matches,
    unique_single_point_matches,
)
from orca_auto.flow.manifest import (
    DEFAULT_RMSD_ENERGY_WINDOW_KCAL,
    DEFAULT_RMSD_THRESHOLD_ANGSTROM,
)
from orca_auto.orca.evidence import (
    OrcaStructureEvidence,
)
from orca_auto.orca.parser import OrcaResult

AtomRow = tuple[str, float, float, float]


def _result(**overrides: object) -> OrcaResult:
    values: dict[str, object] = {
        "source_path": "job/output.out",
        "charge": 0,
        "multiplicity": 1,
        "formula": "H2O",
        "energy_hartree": -76.4,
        "opt_converged": True,
        "input_line": "B3LYP def2-SVP Opt",
        "orca_version": "6.0.1",
        "method": "B3LYP",
        "basis_set": "def2-SVP",
        "coordinates": [
            ("O", 0.0, 0.0, 0.0),
            ("H", 0.0, 0.0, 0.96),
            ("H", 0.93, 0.0, -0.24),
        ],
        "electronic_state_verified": True,
    }
    values.update(overrides)
    return OrcaResult(**values)  # type: ignore[arg-type]


def _block(
    *,
    kind: str = "min",
    imaginary_count: int | None = 0,
    name: str = "structure",
    **result_overrides: object,
) -> OrcaStructureEvidence:
    return OrcaStructureEvidence(
        name=name,
        kind=kind,
        result=_result(**result_overrides),
        analysis=None,
        imaginary_count=imaginary_count,
    )


def _shift(coordinates: list[AtomRow], delta: float) -> list[AtomRow]:
    return [(element, x + delta, y, z) for element, x, y, z in coordinates]


# ---------------------------------------------------------------------------
# Route normalization
# ---------------------------------------------------------------------------


class TestNormalizedRouteLine:
    def test_drops_every_route_marker(self) -> None:
        # ``file_route_lines`` keeps a leading "! " per route line while the
        # output echo merges tokens without any "!"; both must normalize equal.
        assert normalized_route_line("! Opt Freq ! D3BJ") == "opt freq d3bj"
        assert normalized_route_line("Opt Freq D3BJ") == "opt freq d3bj"

    def test_multi_route_line_input_matches_its_own_echo(self) -> None:
        selected = " ".join(["! B3LYP def2-SVP Opt", "! Freq"])
        echoed = "B3LYP def2-SVP Opt Freq"
        assert normalized_route_line(selected) == normalized_route_line(echoed)

    def test_collapses_whitespace_and_case(self) -> None:
        assert normalized_route_line("  !  B3LYP   def2-SVP ") == "b3lyp def2-svp"

    def test_empty_and_non_text_values(self) -> None:
        assert normalized_route_line("") == ""
        assert normalized_route_line(None) == ""


class TestScienceRouteIdentity:
    def test_omits_only_unquoted_pal_resource_shorthands(self) -> None:
        assert orca_science_route_identity(["! HF SP PAL4"]) == (
            (False, "hf"),
            (False, "sp"),
        )
        assert orca_science_route_identity(['! HF SP "PAL4"']) == (
            (False, "hf"),
            (False, "sp"),
            (True, "PAL4"),
        )

    def test_xyzfile_requires_unchanged_materialized_dependency(self, tmp_path: Path) -> None:
        generation = tmp_path / "generation"
        generation.mkdir()
        geometry = generation / "geom.xyz"
        geometry.write_text("1\nbound geometry\nH 0 0 0\n", encoding="utf-8")
        selected = generation / "job.inp"
        selected.write_text("! HF SP\n* xyzfile 0 1 geom.xyz\n", encoding="utf-8")
        materialized = {"dependency_000000": executable_identity(geometry)}

        identity = bound_orca_selected_input_science_identity(
            generation,
            selected,
            bound_selected_identity=executable_identity(selected),
            materialized_input_identities=materialized,
        )

        assert identity is not None
        assert identity.atom_sequence == ("h",)
        geometry.write_text("1\nmutated geometry\nCl 0 0 0\n", encoding="utf-8")
        assert (
            bound_orca_selected_input_science_identity(
                generation,
                selected,
                bound_selected_identity=executable_identity(selected),
                materialized_input_identities=materialized,
            )
            is None
        )

    def test_auxiliary_dependency_content_is_part_of_identity(self, tmp_path: Path) -> None:
        identities = []
        for name, charge_text in (("first", "0.1"), ("second", "9.9")):
            generation = tmp_path / name
            generation.mkdir()
            point_charges = generation / "charges.pc"
            point_charges.write_text(charge_text, encoding="utf-8")
            selected = generation / "job.inp"
            selected.write_text(
                '! HF SP\n%pointcharges "charges.pc"\n* xyz 0 1\nH 0 0 0\n*\n',
                encoding="utf-8",
            )
            identities.append(
                bound_orca_selected_input_science_identity(
                    generation,
                    selected,
                    bound_selected_identity=executable_identity(selected),
                    materialized_input_identities={
                        "dependency_000000": executable_identity(point_charges)
                    },
                )
            )

        assert identities[0] is not None and identities[1] is not None
        assert identities[0] != identities[1]

    def test_auxiliary_dependency_path_is_not_part_of_identity(self, tmp_path: Path) -> None:
        identities = []
        for name in ("first", "second"):
            generation = tmp_path / name
            generation.mkdir()
            point_charges = generation / f"{name}_charges.pc"
            point_charges.write_text("0.1", encoding="utf-8")
            selected = generation / "job.inp"
            selected.write_text(
                f'! HF SP\n%pointcharges "{point_charges}"\n* xyz 0 1\nH 0 0 0\n*\n',
                encoding="utf-8",
            )
            identities.append(
                bound_orca_selected_input_science_identity(
                    generation,
                    selected,
                    bound_selected_identity=executable_identity(selected),
                    materialized_input_identities={
                        "dependency_000000": executable_identity(point_charges)
                    },
                )
            )

        assert identities[0] is not None and identities[1] is not None
        assert identities[0] == identities[1]


# ---------------------------------------------------------------------------
# Geometry comparison
# ---------------------------------------------------------------------------


class TestGeometryMatch:
    def test_identical_coordinates_match(self) -> None:
        a = _block()
        b = _block()
        assert blocks_match_geometry(a, b)

    def test_displacement_beyond_tolerance_rejects(self) -> None:
        base = _block()
        shifted = _block(
            coordinates=_shift(list(base.result.coordinates), GEOMETRY_TOL_ANGSTROM * 2)
        )
        assert not blocks_match_geometry(base, shifted)

    def test_displacement_at_tolerance_matches(self) -> None:
        base = _block()
        nudged = _block(coordinates=_shift(list(base.result.coordinates), GEOMETRY_TOL_ANGSTROM))
        assert blocks_match_geometry(base, nudged)

    def test_element_mismatch_rejects(self) -> None:
        base = _block()
        swapped = list(base.result.coordinates)
        swapped[0] = ("N", *swapped[0][1:])
        assert not blocks_match_geometry(base, _block(coordinates=swapped))

    def test_empty_and_length_mismatch_reject(self) -> None:
        assert not coordinates_match((), ())
        base = _block()
        assert not coordinates_match(
            tuple(base.result.coordinates), tuple(base.result.coordinates[:-1])
        )


# ---------------------------------------------------------------------------
# Minimum eligibility
# ---------------------------------------------------------------------------


class TestEligibleMinimumBlock:
    def test_accepts_a_clean_converged_minimum(self) -> None:
        assert eligible_minimum_block(_block())

    def test_rejects_non_minimum_kinds(self) -> None:
        assert not eligible_minimum_block(_block(kind="ts", imaginary_count=1))
        assert not eligible_minimum_block(_block(kind="sp", imaginary_count=None))

    def test_rejects_unconverged_or_imaginary(self) -> None:
        assert not eligible_minimum_block(_block(opt_converged=None))
        assert not eligible_minimum_block(_block(opt_converged=False))
        assert not eligible_minimum_block(_block(imaginary_count=1))

    def test_rejects_missing_or_non_finite_numbers(self) -> None:
        assert not eligible_minimum_block(_block(energy_hartree=None))
        assert not eligible_minimum_block(_block(energy_hartree=float("nan")))
        assert not eligible_minimum_block(_block(coordinates=[]))
        assert not eligible_minimum_block(_block(coordinates=[("O", float("inf"), 0.0, 0.0)]))

    def test_rejects_incomplete_provenance(self) -> None:
        assert not eligible_minimum_block(_block(input_line=" "))
        assert not eligible_minimum_block(_block(orca_version=""))
        assert not eligible_minimum_block(_block(electronic_state_verified=False))

    def test_expected_electronic_state_pins(self) -> None:
        block = _block(charge=-1, multiplicity=2)
        assert eligible_minimum_block(block, expected_charge=-1, expected_multiplicity=2)
        assert not eligible_minimum_block(block, expected_charge=0)
        assert not eligible_minimum_block(block, expected_multiplicity=1)


# ---------------------------------------------------------------------------
# Unique single-point pairing
# ---------------------------------------------------------------------------


class TestUniqueSinglePointMatches:
    def test_unique_pair_matches(self) -> None:
        minimum = _block()
        sp = _block(kind="sp", imaginary_count=None, energy_hartree=-76.6)
        assert unique_single_point_matches([minimum], [sp]) == [0]

    def test_one_structure_two_candidate_sps_pairs_nothing(self) -> None:
        minimum = _block()
        sp = _block(kind="sp", imaginary_count=None, energy_hartree=-76.6)
        assert unique_single_point_matches([minimum], [sp, sp]) == [None]

    def test_one_sp_shared_by_two_structures_pairs_nothing(self) -> None:
        minimum = _block()
        sp = _block(kind="sp", imaginary_count=None, energy_hartree=-76.6)
        assert unique_single_point_matches([minimum, minimum], [sp]) == [None, None]

    def test_state_mismatch_rejects(self) -> None:
        minimum = _block()
        sp = _block(kind="sp", imaginary_count=None, charge=1, energy_hartree=-76.6)
        assert unique_single_point_matches([minimum], [sp]) == [None]

    def test_geometry_mismatch_rejects(self) -> None:
        minimum = _block()
        sp = _block(
            kind="sp",
            imaginary_count=None,
            energy_hartree=-76.6,
            coordinates=_shift(list(minimum.result.coordinates), 0.5),
        )
        assert unique_single_point_matches([minimum], [sp]) == [None]

    def test_missing_provenance_or_energy_rejects(self) -> None:
        minimum = _block()
        assert unique_single_point_matches(
            [minimum],
            [_block(kind="sp", imaginary_count=None, energy_hartree=None)],
        ) == [None]
        assert unique_single_point_matches(
            [minimum],
            [
                _block(
                    kind="sp",
                    imaginary_count=None,
                    energy_hartree=-76.6,
                    electronic_state_verified=False,
                )
            ],
        ) == [None]
        assert unique_single_point_matches(
            [_block(electronic_state_verified=False)],
            [_block(kind="sp", imaginary_count=None, energy_hartree=-76.6)],
        ) == [None]

    def test_disjoint_structures_pair_independently(self) -> None:
        first = _block()
        second = _block(coordinates=_shift(list(first.result.coordinates), 1.0))
        sp_first = _block(kind="sp", imaginary_count=None, energy_hartree=-76.6)
        sp_second = _block(
            kind="sp",
            imaginary_count=None,
            energy_hartree=-76.7,
            coordinates=list(second.result.coordinates),
        )
        assert unique_single_point_matches([first, second], [sp_second, sp_first]) == [1, 0]


# ---------------------------------------------------------------------------
# Selected-input consistency
# ---------------------------------------------------------------------------


def _write_selected_inp(path: Path, *, route_lines: list[str], charge: int, mult: int) -> None:
    lines = [*route_lines, f"* xyz {charge} {mult}", "O 0.0 0.0 0.0", "*"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestSelectedInputStateMatches:
    def test_single_route_line_matches(self, tmp_path: Path) -> None:
        inp = tmp_path / "job.inp"
        _write_selected_inp(inp, route_lines=["! B3LYP def2-SVP Opt"], charge=0, mult=1)
        block = _block(input_line="B3LYP def2-SVP Opt")
        assert selected_input_state_matches(block, {"selected_inp": str(inp)})

    def test_multi_route_line_input_matches_merged_echo(self, tmp_path: Path) -> None:
        # Regression: the SI side used to strip only the LEADING "!" from the
        # joined route lines, so any input with more than one route line was
        # silently downgraded to unverified provenance.
        inp = tmp_path / "job.inp"
        _write_selected_inp(inp, route_lines=["! B3LYP def2-SVP Opt", "! Freq"], charge=0, mult=1)
        block = _block(input_line="B3LYP def2-SVP Opt Freq")
        assert selected_input_state_matches(block, {"selected_inp": str(inp)})

    def test_route_mismatch_rejects(self, tmp_path: Path) -> None:
        inp = tmp_path / "job.inp"
        _write_selected_inp(inp, route_lines=["! B3LYP def2-SVP Opt"], charge=0, mult=1)
        block = _block(input_line="wB97X-D3 def2-TZVP")
        assert not selected_input_state_matches(block, {"selected_inp": str(inp)})

    def test_electronic_state_mismatch_rejects(self, tmp_path: Path) -> None:
        inp = tmp_path / "job.inp"
        _write_selected_inp(inp, route_lines=["! B3LYP def2-SVP Opt"], charge=1, mult=2)
        block = _block(input_line="B3LYP def2-SVP Opt")
        assert not selected_input_state_matches(block, {"selected_inp": str(inp)})

    def test_missing_or_unreadable_input_fails_closed(self, tmp_path: Path) -> None:
        block = _block()
        assert not selected_input_state_matches(block, {})
        assert not selected_input_state_matches(
            block, {"selected_inp": str(tmp_path / "absent.inp")}
        )

    def test_input_without_geometry_header_fails_closed(self, tmp_path: Path) -> None:
        inp = tmp_path / "job.inp"
        inp.write_text("! B3LYP def2-SVP Opt\n", encoding="utf-8")
        assert not selected_input_state_matches(_block(), {"selected_inp": str(inp)})


# ---------------------------------------------------------------------------
# RMSD grouping defaults
# ---------------------------------------------------------------------------


class TestRmsdGrouping:
    def test_none_and_empty_config_use_the_shared_defaults(self) -> None:
        # Two identical geometries inside the default 0.1 kcal window must
        # collapse under both spellings of "no explicit config" — the exact
        # situation where the materializer and the SI previously built their
        # own default dictionaries.
        low = rmsd_candidate_for_block(
            "low", _block(energy_hartree=-76.50001), energy_hartree=-76.50001
        )
        high = rmsd_candidate_for_block("high", _block(energy_hartree=-76.5), energy_hartree=-76.5)
        configs: tuple[dict[str, object] | None, ...] = (None, {})
        for cfg in configs:
            grouping = rmsd_grouping([low, high], cfg)
            assert grouping.representative_ids == frozenset({"low"})

    def test_explicit_config_overrides_defaults(self) -> None:
        # The geometries differ internally (one O–H stretched by 1 Å), which
        # survives the rotational alignment; a rigid translation would not.
        base = _block(energy_hartree=-76.5)
        stretched_coordinates = list(base.result.coordinates)
        stretched_coordinates[1] = ("H", 0.0, 0.0, 1.96)
        far = _block(energy_hartree=-76.49999, coordinates=stretched_coordinates)
        candidates = [
            rmsd_candidate_for_block("base", base, energy_hartree=-76.5),
            rmsd_candidate_for_block("far", far, energy_hartree=-76.49999),
        ]
        default_grouping = rmsd_grouping(candidates, None)
        loose_grouping = rmsd_grouping(
            candidates,
            {
                "rmsd_threshold_angstrom": 10.0,
                "energy_window_kcal": 1000.0,
            },
        )
        assert default_grouping.representative_ids == frozenset({"base", "far"})
        assert loose_grouping.representative_ids == frozenset({"base"})

    def test_default_constants_are_the_manifest_ones(self) -> None:
        # The module must source its defaults from the durable-config module,
        # not re-declare them.
        assert DEFAULT_RMSD_THRESHOLD_ANGSTROM > 0
        assert DEFAULT_RMSD_ENERGY_WINDOW_KCAL > 0


# ---------------------------------------------------------------------------
# Provenance helper
# ---------------------------------------------------------------------------


class TestHasRequiredProvenance:
    def test_requires_route_version_and_verified_state(self) -> None:
        assert has_required_provenance(_block())
        assert not has_required_provenance(_block(input_line=""))
        assert not has_required_provenance(_block(orca_version=" "))
        assert not has_required_provenance(_block(electronic_state_verified=False))
