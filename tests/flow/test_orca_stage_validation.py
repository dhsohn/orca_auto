from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.flow.orca_stage_validation import (
    ensure_route_line,
    validate_workflow_orca_input_bytes,
    validate_workflow_orca_route,
    validate_workflow_orca_task_kind,
)


@pytest.mark.parametrize("route_line", ["", "   ", "\n\n", "# only a comment"])
def test_blank_route_line_refuses_instead_of_substituting_a_level_of_theory(
    route_line: str,
) -> None:
    # A durable payload without an active route must fail its cycle rather than
    # render at whatever level of theory happens to be hard-coded here.
    with pytest.raises(ValueError, match="route_line has no active route"):
        ensure_route_line(route_line)


@pytest.mark.parametrize(
    "route_line",
    (
        '! "OptTS" Freq r2scan-3c',
        '! OptTS "Freq" r2scan-3c',
        '! OptTS "NumFreq" r2scan-3c',
        '! OptTS "AnFreq" r2scan-3c',
    ),
)
def test_workflow_route_rejects_quoted_program_keywords(route_line: str) -> None:
    with pytest.raises(ValueError, match="quoted tokens"):
        validate_workflow_orca_route(task_kind="optts_freq", route_line=route_line)


@pytest.mark.parametrize(
    "route_line",
    (
        "!! Opt r2scan-3c",
        "!%pal Opt r2scan-3c",
        "! Opt %pal nprocs 8 end",
        "! Opt * xyz 0 1",
        "! Opt $new_job",
    ),
)
def test_workflow_route_rejects_marker_prefixed_payload_tokens(route_line: str) -> None:
    with pytest.raises(ValueError, match="marker-prefixed payload token"):
        validate_workflow_orca_route(task_kind="opt", route_line=route_line)


def test_workflow_route_accepts_compact_leading_bang_keyword() -> None:
    assert (
        validate_workflow_orca_route(task_kind="opt", route_line="!B3LYP Opt TightSCF")
        == "! B3LYP Opt TightSCF"
    )


@pytest.mark.parametrize(
    ("task_kind", "route_line"),
    (
        ("opt", "! HF fake-Opt TightSCF"),
        ("optts_freq", "! HF OptTS fake-Freq TightSCF"),
        ("optts_freq", "! HF OptTS Freq(foo) TightSCF"),
    ),
)
def test_workflow_route_requires_full_keyword_tokens(
    task_kind: str,
    route_line: str,
) -> None:
    with pytest.raises(ValueError, match="route-role mismatch"):
        validate_workflow_orca_route(task_kind=task_kind, route_line=route_line)


@pytest.mark.parametrize(
    "optimization_keyword",
    ("Opt", "TightOpt", "COpt", "ZOpt", "VeryTightOpt"),
)
def test_workflow_opt_route_accepts_supported_exact_optimization_tokens(
    optimization_keyword: str,
) -> None:
    route_line = f"! HF {optimization_keyword} TightSCF"
    assert validate_workflow_orca_route(task_kind="opt", route_line=route_line) == route_line


def test_workflow_sp_route_does_not_invent_bare_ts_run_type() -> None:
    route_line = "! HF TS TightSCF"
    assert validate_workflow_orca_route(task_kind="sp", route_line=route_line) == route_line


def test_route_role_validation_matches_multiline_route_rendering() -> None:
    with pytest.raises(ValueError, match="route-role mismatch"):
        validate_workflow_orca_route(
            task_kind="optts_freq",
            route_line="SP\nOptTS Freq",
        )

    assert (
        validate_workflow_orca_route(
            task_kind="optts_freq",
            route_line="! SP\n! OptTS Freq",
        )
        == "! SP\n! OptTS Freq"
    )


@pytest.mark.parametrize(
    "route_line",
    [
        "! OptTS Freq r2scan-3c\n%geom\n  MaxIter 999\nend",
        "! OptTS Freq r2scan-3c\n* xyz 0 1\nH 0 0 0\n*",
        "! OptTS Freq r2scan-3c\nPAL8",
    ],
)
def test_workflow_route_rejects_active_non_route_lines(route_line: str) -> None:
    with pytest.raises(ValueError, match="may contain only active '!' route lines"):
        validate_workflow_orca_route(task_kind="optts_freq", route_line=route_line)


def test_unknown_task_kind_error_is_exact() -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_workflow_orca_task_kind(" geometry_opt ")

    assert str(exc_info.value) == (
        "unsupported workflow ORCA task_kind: 'geometry_opt'; "
        "expected one of ['opt', 'optts_freq', 'relaxed_scan', 'sp']"
    )


def test_sp_route_error_preserves_forbidden_token_order() -> None:
    route_line = "! HF MD Opt NumFreq"

    with pytest.raises(ValueError) as exc_info:
        validate_workflow_orca_route(task_kind="sp", route_line=route_line)

    assert str(exc_info.value) == (
        "workflow ORCA route-role mismatch: task_kind='sp' requires a pure "
        "single-point route without non-energy run types; "
        "forbidden_tokens=['MD', 'Opt', 'NumFreq']; "
        "route_line='! HF MD Opt NumFreq'"
    )


def test_invalid_utf8_input_reports_the_exact_missing_route_reason(tmp_path: Path) -> None:
    inp_path = tmp_path / "invalid.inp"

    with pytest.raises(ValueError) as exc_info:
        validate_workflow_orca_input_bytes(
            task_kind="opt",
            inp_path=inp_path,
            input_bytes=b"\xff",
        )

    assert str(exc_info.value) == (
        "workflow ORCA route-role mismatch: task_kind='opt' requires a readable "
        f"selected input with an active route line; inp_path={str(inp_path)!r}"
    )


def test_route_role_error_precedes_relaxed_scan_geometry_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_workflow_orca_input_bytes(
            task_kind="relaxed_scan",
            inp_path=tmp_path / "invalid_scan.inp",
            input_bytes=b"! SP HF\n* xyz 0 1\ninvalid geometry\n*\n",
        )

    assert "requires a non-TS geometry optimization" in str(exc_info.value)
    assert "requires a %geom Scan block" not in str(exc_info.value)


def test_bytes_validator_uses_supplied_bytes_instead_of_disk_input(tmp_path: Path) -> None:
    inp_path = tmp_path / "selected.inp"
    inp_path.write_text("! SP HF\n", encoding="utf-8")

    validated_route = validate_workflow_orca_input_bytes(
        task_kind="opt",
        inp_path=inp_path,
        input_bytes=b"! Opt HF\n* xyz 0 1\nH 0 0 0\n*\n",
    )

    assert validated_route == "! Opt HF"


def test_bytes_validator_resolves_xyzfile_from_prospective_input_parent(
    tmp_path: Path,
) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "geometry.xyz").write_text(
        "2\ngeometry\nH 0 0 0\nH 0 0 0.74\n",
        encoding="utf-8",
    )
    input_bytes = (
        b"! Opt HF\n"
        b"%geom\n"
        b"  Scan\n"
        b"    B 0 1 = 0.7, 2.0, 8\n"
        b"  end\n"
        b"end\n"
        b"* xyzfile 0 1 geometry.xyz\n"
    )

    validated_route = validate_workflow_orca_input_bytes(
        task_kind="relaxed_scan",
        inp_path=snapshot_dir / "selected.inp",
        input_bytes=input_bytes,
    )

    assert validated_route == "! Opt HF"
