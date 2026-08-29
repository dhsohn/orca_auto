from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.flow.workflow import report_collection, report_energy_evidence
from orca_auto.flow.workflow.report_energy_evidence import (
    latest_engrad_energy,
    orca_report_output_energy_state,
)
from tests.flow.workflow_report_helpers import _ENGRAD_TEMPLATE, _orca_output_report


def test_report_collection_does_not_forward_energy_evidence_owner() -> None:
    for name in report_energy_evidence.__all__:
        assert not hasattr(report_collection, name), name


def test_engrad_energy(tmp_path: Path) -> None:
    (tmp_path / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-100.123456789012"), encoding="utf-8"
    )
    assert latest_engrad_energy(tmp_path) == pytest.approx(-100.123456789012)


def test_engrad_energy_rejects_non_finite_values(tmp_path: Path) -> None:
    # A corrupt .engrad spelling nan would render as NaN in the report and
    # then crash the machine-observation writer (allow_nan=False) on every
    # advance; a non-finite energy must read as unavailable instead.
    (tmp_path / "opt.engrad").write_text(_ENGRAD_TEMPLATE.format(energy="nan"), encoding="utf-8")
    assert latest_engrad_energy(tmp_path) is None


@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_engrad_energy_rejects_linked_generation_file(
    tmp_path: Path,
    link_kind: str,
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    foreign = tmp_path / "foreign.engrad"
    foreign.write_text(_ENGRAD_TEMPLATE.format(energy="-999.0"), encoding="utf-8")
    linked = generation / "linked.engrad"
    if link_kind == "symlink":
        linked.symlink_to(foreign)
    else:
        os.link(foreign, linked)

    assert latest_engrad_energy(generation) is None


def test_engrad_energy_rejects_oversized_generation_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engrad = tmp_path / "oversized.engrad"
    engrad.write_text(
        _ENGRAD_TEMPLATE.format(energy="-100.0") + "x" * 128,
        encoding="utf-8",
    )
    monkeypatch.setattr(report_energy_evidence, "_MAX_ENGRAD_ENERGY_FILE_BYTES", 64)

    assert latest_engrad_energy(tmp_path) is None


def test_orca_output_energy_reads_only_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_large_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_bytes(
        b"FINAL SINGLE POINT ENERGY -9.000000000000\n"
        + b"x" * (report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
        + b"\nFINAL SINGLE POINT ENERGY -2.500000000000\n"
    )
    bytes_requested = 0
    original_pread = report_energy_evidence.os.pread

    def tracked_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal bytes_requested
        bytes_requested += count
        return original_pread(descriptor, count, offset)

    monkeypatch.setattr(report_energy_evidence.os, "pread", tracked_pread)

    _annotated, energy = orca_report_output_energy_state(stage_dir, _orca_output_report(out_path))

    assert out_path.stat().st_size > report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES
    assert bytes_requested == report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES
    assert energy == pytest.approx(-2.5)


def test_orca_output_energy_sees_annotated_line_cut_at_window_start(tmp_path: Path) -> None:
    # A line whose first byte lands exactly on a window's start byte is
    # skipped there as possibly truncated; the next window's overlap must
    # re-read it whole and still report the annotation.
    stage_dir = tmp_path / "orca_window_boundary"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    prefix = b"|  1> ! r2scan-3c Opt Freq TightSCF\n"
    annotated_line = b"FINAL SINGLE POINT ENERGY -1.100000000000 (SCF not fully converged!)\n"
    out_path.write_bytes(
        prefix
        + annotated_line
        + b"x" * (report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES - len(annotated_line))
    )
    assert out_path.stat().st_size - report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES == len(
        prefix
    )

    annotated_state, energy = orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    )

    assert annotated_state is True
    assert energy is None


def test_orca_output_energy_skips_false_match_at_mid_line_window_start(tmp_path: Path) -> None:
    # A window can begin mid-line. When the cut lands right before an
    # energy-line echo embedded in a longer line, the buffer-position-0
    # match is a complete-looking impostor the full file never matches;
    # the skip rule must reject it so the true line's value publishes.
    stage_dir = tmp_path / "orca_mid_line_cut"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    real_line = b"FINAL SINGLE POINT ENERGY -1.100000000000\n"
    echo_head = b"| 27> "
    fake_tail = b"FINAL SINGLE POINT ENERGY -9.900000000000\n"
    out_path.write_bytes(
        real_line
        + echo_head
        + fake_tail
        + b"x" * (report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES - len(fake_tail))
    )
    assert out_path.stat().st_size - report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES == len(
        real_line
    ) + len(echo_head)

    annotated_state, energy = orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    )

    assert annotated_state is False
    assert energy == pytest.approx(-1.1)


@pytest.mark.parametrize("final_state", ("missing", "no_energy_line"))
def test_orca_output_energy_refuses_earlier_attempt_for_recorded_final(
    tmp_path: Path, final_state: str
) -> None:
    # A recorded final output is authoritative. The verified report
    # resolution already rejects a report whose bound final output is
    # missing, so this chain sees that shape only in the window between
    # verification and the scan — and an earlier attempt's clean value must
    # not stand in for the final geometry there, nor when the final is
    # readable but prints no final energy line.
    stage_dir = tmp_path / "orca_final_authority"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text("FINAL SINGLE POINT ENERGY -1.000000000000\n", encoding="utf-8")
    final_out = stage_dir / "final.out"
    if final_state == "no_energy_line":
        final_out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(final_out),
            },
        }
    }

    annotated_state, energy = orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is False
    assert energy is None


def test_orca_output_energy_keeps_attempt_annotation_evidence_for_recorded_final(
    tmp_path: Path,
) -> None:
    # The conservative edge stays: with the recorded final unreadable, an
    # annotated earlier attempt still taints the chain, so the retained
    # engrad is refused rather than published unverifiable.
    stage_dir = tmp_path / "orca_final_authority_annotated"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text(
        "FINAL SINGLE POINT ENERGY -1.000000000000 (SCF not fully converged!)\n",
        encoding="utf-8",
    )
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(stage_dir / "vanished.out"),
            },
        }
    }

    annotated_state, energy = orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is True
    assert energy is None


def test_orca_output_energy_scans_attempts_when_no_final_was_recorded(tmp_path: Path) -> None:
    # Records that never captured a final output path keep the attempt scan,
    # exactly like the per-job rule.
    stage_dir = tmp_path / "orca_never_recorded"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text("FINAL SINGLE POINT ENERGY -1.000000000000\n", encoding="utf-8")
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {"reason": "normal_termination"},
        }
    }

    annotated_state, energy = orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is False
    assert energy == pytest.approx(-1.0)


def test_orca_output_energy_rejects_file_changed_during_tail_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_changing_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    original_pread = report_energy_evidence.os.pread
    changed = False

    def mutating_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal changed
        chunk = original_pread(descriptor, count, offset)
        if not changed:
            changed = True
            with out_path.open("ab") as handle:
                handle.write(b"changed\n")
        return chunk

    monkeypatch.setattr(report_energy_evidence.os, "pread", mutating_pread)

    assert orca_report_output_energy_state(stage_dir, _orca_output_report(out_path)) == (
        False,
        None,
    )


def test_orca_output_energy_rejects_nonregular_multilink_or_unconfined_paths(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "orca_untrusted_output"
    stage_dir.mkdir()
    target = stage_dir / "target.out"
    target.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    symlink = stage_dir / "symlink.out"
    symlink.symlink_to(target.name)
    hardlink = stage_dir / "hardlink.out"
    os.link(target, hardlink)
    fifo = stage_dir / "fifo.out"
    os.mkfifo(fifo)
    outside = tmp_path / "outside.out"
    outside.write_text(
        "FINAL SINGLE POINT ENERGY -2.200000000000\n",
        encoding="utf-8",
    )

    for candidate in (symlink, hardlink, fifo, outside):
        assert orca_report_output_energy_state(stage_dir, _orca_output_report(candidate)) == (
            False,
            None,
        )
