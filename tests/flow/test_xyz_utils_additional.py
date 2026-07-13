from __future__ import annotations

import math
from pathlib import Path

import pytest

from orca_auto.flow import xyz_utils


@pytest.mark.parametrize(
    ("symbol", "charge", "uhf", "message"),
    [
        ("Xx", 0, 0, "Unknown element"),
        ("Og", 0, 0, "atomic-number"),
        ("H", 2, 0, "negative electron"),
        ("H", 0, 0, "parity"),
        ("H", 0, 2, "between 0 and the electron count"),
    ],
)
def test_validate_electronic_state_rejects_unsupported_or_inconsistent_state(
    tmp_path: Path,
    symbol: str,
    charge: int,
    uhf: int,
    message: str,
) -> None:
    xyz_path = tmp_path / "state.xyz"
    xyz_path.write_text(f"1\nstate\n{symbol} 0 0 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        xyz_utils.validate_electronic_state(xyz_path, charge=charge, uhf=uhf)


def test_validate_electronic_state_accepts_hydrogen_doublet(tmp_path: Path) -> None:
    xyz_path = tmp_path / "h.xyz"
    xyz_path.write_text("1\ndoublet\nH 0 0 0\n", encoding="utf-8")

    assert xyz_utils.validate_electronic_state(xyz_path, charge=0, uhf=1) == {
        "nuclear_charge": 1,
        "electron_count": 1,
        "charge": 0,
        "uhf": 1,
    }


def test_load_xyz_frames_rejects_missing_invalid_and_truncated_inputs(tmp_path: Path) -> None:
    missing = tmp_path / "missing.xyz"
    invalid_header = tmp_path / "invalid_header.xyz"
    invalid_header.write_text("not_a_number\ncomment\nH 0 0 0\n", encoding="utf-8")
    invalid_tokens = tmp_path / "invalid_tokens.xyz"
    invalid_tokens.write_text("1\ncomment\nH nope 0 0\n", encoding="utf-8")
    truncated = tmp_path / "truncated.xyz"
    truncated.write_text("2\ncomment\nH 0 0 0\n", encoding="utf-8")
    non_positive = tmp_path / "non_positive.xyz"
    non_positive.write_text("0\ncomment\n", encoding="utf-8")

    assert xyz_utils.load_xyz_frames(missing) == ()
    assert xyz_utils.load_xyz_frames(invalid_header) == ()
    assert xyz_utils.load_xyz_frames(invalid_tokens) == ()
    assert xyz_utils.load_xyz_frames(truncated) == ()
    assert xyz_utils.load_xyz_frames(non_positive) == ()
    assert xyz_utils.has_xyz_geometry(invalid_tokens) is False


@pytest.mark.parametrize("coordinate", ["nan", "inf", "-inf"])
def test_load_xyz_frames_rejects_nonfinite_coordinates(tmp_path: Path, coordinate: str) -> None:
    xyz_path = tmp_path / "nonfinite.xyz"
    xyz_path.write_text(
        f"2\ninvalid geometry\nH {coordinate} 0 0\nH 0 0 0.74\n",
        encoding="utf-8",
    )

    result = xyz_utils.parse_xyz_file(xyz_path)

    assert result.ok is False
    assert result.error_reason == "invalid_atom_line"
    assert xyz_utils.load_xyz_frames(xyz_path) == ()


@pytest.mark.parametrize("coordinate", [math.nan, math.inf, -math.inf])
def test_write_fragment_xyz_rejects_nonfinite_coordinates(
    tmp_path: Path, coordinate: float
) -> None:
    target = tmp_path / "fragment.xyz"

    with pytest.raises(ValueError, match="non-finite coordinates"):
        xyz_utils.write_fragment_xyz(
            coordinates=[("H", coordinate, 0.0, 0.0)],
            atom_indices=[0],
            target_path=target,
        )

    assert not target.exists()


def test_load_xyz_frames_extracts_energy_and_render_round_trips(tmp_path: Path) -> None:
    xyz_path = tmp_path / "frames.xyz"
    xyz_path.write_text(
        "\n".join(
            [
                "2",
                "energy: -1.25",
                "H 0 0 0",
                "H 0 0 0.74",
                "",
                "2",
                "E 3.5",
                "H 0.1 0 0",
                "H 0.1 0 0.74",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    frames = xyz_utils.load_xyz_frames(xyz_path)

    assert len(frames) == 2
    assert frames[0].energy == -1.25
    assert frames[1].energy == 3.5
    assert frames[0].render().startswith("2\nenergy: -1.25\n")


def test_load_xyz_frames_parses_scientific_notation_energy_without_truncation(
    tmp_path: Path,
) -> None:
    xyz_path = tmp_path / "scientific.xyz"
    xyz_path.write_text(
        "1\nenergy: -1.0E+02\nH 0 0 0\n1\nenergy: -9.0E+01\nH 0.1 0 0\n",
        encoding="utf-8",
    )

    frames = xyz_utils.load_xyz_frames(xyz_path)

    assert [frame.energy for frame in frames] == [-100.0, -90.0]
    selected, metadata = xyz_utils.choose_orca_geometry_frame(
        xyz_path,
        candidate_kind="selected_path",
    )
    assert selected is not None and selected.index == 2
    assert metadata["selected_frame_energy"] == -90.0


def test_load_xyz_atom_sequence_raises_for_invalid_or_multiframe_input(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.xyz"
    invalid.write_text("bad\n", encoding="utf-8")
    multiframe = tmp_path / "multi.xyz"
    multiframe.write_text(
        "1\none\nH 0 0 0\n1\ntwo\nH 0.1 0 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid or empty XYZ file"):
        xyz_utils.load_xyz_atom_sequence(invalid)
    with pytest.raises(ValueError, match="Expected a single-geometry XYZ file"):
        xyz_utils.load_xyz_atom_sequence(multiframe)


def test_choose_orca_geometry_frame_covers_invalid_single_highest_middle_and_first(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.xyz"
    invalid.write_text("bad\n", encoding="utf-8")
    ts_multiframe = tmp_path / "ts_multi.xyz"
    ts_multiframe.write_text(
        "1\nenergy: 1.0\nH 0 0 0\n1\nenergy: 2.0\nH 0.1 0 0\n",
        encoding="utf-8",
    )
    energetic = tmp_path / "energetic.xyz"
    energetic.write_text(
        "1\nenergy: -5.0\nH 0 0 0\n1\nenergy: -1.0\nH 0.1 0 0\n1\nenergy: -3.0\nH 0.2 0 0\n",
        encoding="utf-8",
    )
    no_energy = tmp_path / "no_energy.xyz"
    no_energy.write_text(
        "1\nfirst\nH 0 0 0\n1\nsecond\nH 0.1 0 0\n1\nthird\nH 0.2 0 0\n",
        encoding="utf-8",
    )
    multi_default = tmp_path / "multi_default.xyz"
    multi_default.write_text(
        "1\nenergy: -4.0\nH 0 0 0\n1\nenergy: -2.0\nH 0.1 0 0\n",
        encoding="utf-8",
    )

    frame, metadata = xyz_utils.choose_orca_geometry_frame(invalid, candidate_kind="ts_guess")
    assert frame is None
    assert metadata["selection_reason"] == "invalid_or_empty_xyz"

    frame, metadata = xyz_utils.choose_orca_geometry_frame(ts_multiframe, candidate_kind="ts_guess")
    assert frame is None
    assert metadata["selection_reason"] == "ts_guess_requires_single_frame"

    frame, metadata = xyz_utils.choose_orca_geometry_frame(
        energetic, candidate_kind="selected_path"
    )
    assert frame is not None
    assert frame.index == 2
    assert metadata["selection_reason"] == "highest_energy_frame"
    assert metadata["selected_frame_energy"] == -1.0

    frame, metadata = xyz_utils.choose_orca_geometry_frame(
        no_energy, candidate_kind="selected_path"
    )
    assert frame is not None
    assert frame.index == 2
    assert metadata["selection_reason"] == "middle_frame_fallback"

    frame, metadata = xyz_utils.choose_orca_geometry_frame(
        multi_default, candidate_kind="optimized_geometry"
    )
    assert frame is not None
    assert frame.index == 1
    assert metadata["selection_reason"] == "first_frame"
    assert metadata["selected_frame_energy"] == -4.0


def test_write_orca_ready_xyz_materializes_selected_frame_and_raises_on_invalid_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "path.xyz"
    source.write_text(
        "1\nenergy: -3.0\nH 0 0 0\n1\nenergy: -1.0\nH 0.1 0 0\n",
        encoding="utf-8",
    )
    target = tmp_path / "materialized" / "orca.xyz"

    metadata = xyz_utils.write_orca_ready_xyz(
        source_path=source,
        target_path=target,
        candidate_kind="selected_path",
    )

    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("1\nenergy: -1.0\n")
    assert metadata["selection_reason"] == "highest_energy_frame"
    assert metadata["materialized_xyz_path"] == str(target.resolve())

    with pytest.raises(ValueError, match="No ORCA-ready XYZ geometry found"):
        xyz_utils.write_orca_ready_xyz(
            source_path=tmp_path / "missing.xyz",
            target_path=tmp_path / "out.xyz",
            candidate_kind="ts_guess",
        )


def test_write_orca_ready_xyz_materializes_requested_multiframe_index(tmp_path: Path) -> None:
    source = tmp_path / "crest_conformers.xyz"
    source.write_text(
        "1\nconf 1\nH 0 0 0\n1\nconf 2\nH 0.2 0 0\n1\nconf 3\nH 0.3 0 0\n",
        encoding="utf-8",
    )
    target = tmp_path / "orca" / "conformer_guess.xyz"

    metadata = xyz_utils.write_orca_ready_xyz(
        source_path=source,
        target_path=target,
        candidate_kind="crest_conformer",
        source_frame_index=2,
    )

    assert target.read_text(encoding="utf-8").startswith("1\nconf 2\n")
    assert metadata["requested_frame_index"] == 2
    assert metadata["selected_frame_index"] == 2
    assert metadata["selection_reason"] == "requested_frame"


@pytest.mark.parametrize(
    "comment",
    [
        "host\nH 99 98 97",
        "host\rguest",
        "host\x00guest",
        "host\u0085H 99 98 97",
        "host\u2028H 99 98 97",
        "host\u2029H 99 98 97",
    ],
)
def test_write_fragment_xyz_rejects_multiline_or_control_comments(
    tmp_path: Path, comment: str
) -> None:
    target = tmp_path / "fragment.xyz"
    with pytest.raises(ValueError, match="comment"):
        xyz_utils.write_fragment_xyz(
            coordinates=[("Cl", 0.0, 0.0, 0.0)],
            atom_indices=[0],
            target_path=target,
            comment=comment,
        )
    assert not target.exists()
