"""``execution_binding._capture``: the one boundary for source bytes and byte budgets.

Every binding stage reads the bytes it binds or verifies through
``_capture.capture`` and checks its budgets through ``_capture``. A test that
injects a read failure or shrinks a budget therefore patches one module,
whatever stage reads the file, and the bytes a stage verifies are the bytes it
uses.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

import orca_auto.orca.execution_binding as binding_package
from orca_auto.orca.execution_binding import _capture, verify_orca_execution_snapshot
from tests.conftest import build_submitted_snapshot, write_fake_orca

_BOUNDARY_NAMES = frozenset(
    {"read_stable_regular_file", "MAX_INPUT_SNAPSHOT_BYTES", "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES"}
)
_PACKAGE_DIR = Path(binding_package.__file__).parent
# The package re-exports the aggregate limit for runtime consumers.
_ALLOWED = {"_capture.py": _BOUNDARY_NAMES, "__init__.py": {"MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES"}}


def _names_used(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, ast.Attribute) and not (
            isinstance(node.value, ast.Name) and node.value.id == "_capture"
        ):
            names.add(node.attr)
    return names


@pytest.mark.parametrize("module", sorted(path.name for path in _PACKAGE_DIR.glob("*.py")))
def test_only_the_capture_module_reads_source_bytes_or_holds_a_budget(module: str) -> None:
    used = _names_used(_PACKAGE_DIR / module) & _BOUNDARY_NAMES

    assert used <= _ALLOWED.get(module, set())


def _job(tmp_path: Path) -> tuple[Path, Path, Path]:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "h2.xyz").write_text("2\nH2\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    selected = job_dir / "h2.inp"
    # A long comment so the bound input, with the geometry inlined, outgrows
    # a small per-source budget while each source stays within it.
    selected.write_text(
        "! HF STO-3G Opt\n#" + "x" * 200 + "\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8"
    )
    return job_dir, selected, write_fake_orca(tmp_path / "orca")


def _build(job_dir: Path, selected: Path, executable: Path) -> dict[str, Any]:
    return build_submitted_snapshot(
        job_dir,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 1, "max_memory_gb": 1},
        orca_executable=executable,
    )


def _verify(job_dir: Path, snapshot: dict[str, Any]) -> None:
    verify_orca_execution_snapshot(
        job_dir,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz="",
        expected_resource_request={"max_cores": 1, "max_memory_gb": 1},
    )


def test_claim_time_verification_parses_the_bytes_it_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, selected, executable = _job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    bound = Path(snapshot["bound_selected_identity"]["path"]).resolve()
    real_read = _capture.read_stable_regular_file
    reads: list[Path] = []

    def recorded(path: Path, **kwargs: Any) -> bytes:
        if Path(path).resolve() == bound:
            reads.append(Path(path))
        return real_read(path, **kwargs)

    monkeypatch.setattr(_capture, "read_stable_regular_file", recorded)

    _verify(job_dir, snapshot)

    # One read of the bound input: its identity check and its parse share it.
    assert len(reads) == 1


def test_claim_time_verification_refuses_bytes_other_than_the_bound_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, selected, executable = _job(tmp_path)
    snapshot = _build(job_dir, selected, executable)
    bound = Path(snapshot["bound_selected_identity"]["path"]).resolve()
    real_read = _capture.read_stable_regular_file

    def substituted(path: Path, **kwargs: Any) -> bytes:
        payload = real_read(path, **kwargs)
        if Path(path).resolve() == bound:
            return payload.replace(b"Opt", b"SP ")
        return payload

    monkeypatch.setattr(_capture, "read_stable_regular_file", substituted)

    with pytest.raises(ValueError, match="bound selected input snapshot is corrupt"):
        _verify(job_dir, snapshot)


def test_a_bound_input_larger_than_one_source_budget_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each source fits the per-source budget; the bound input, with the
    # geometry inlined, does not. It is capped by the aggregate budget only.
    monkeypatch.setattr(_capture, "MAX_INPUT_SNAPSHOT_BYTES", 240)
    monkeypatch.setattr(_capture, "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES", 960)
    job_dir, selected, executable = _job(tmp_path)
    assert selected.stat().st_size <= 240
    snapshot = _build(job_dir, selected, executable)
    assert Path(snapshot["bound_selected_identity"]["path"]).stat().st_size > 240

    _verify(job_dir, snapshot)


def test_one_budget_patch_reaches_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, selected, executable = _job(tmp_path)
    monkeypatch.setattr(_capture, "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES", selected.stat().st_size)

    with pytest.raises(ValueError, match="aggregate snapshot size"):
        _build(job_dir, selected, executable)


def test_captured_identity_describes_exactly_the_bytes_read(tmp_path: Path) -> None:
    path = tmp_path / "input.xyz"
    path.write_bytes(b"1\nH\nH 0 0 0\n")

    captured = _capture.capture(path)

    assert captured.payload == b"1\nH\nH 0 0 0\n"
    assert captured.identity() == {
        "path": str(path),
        "sha256": hashlib.sha256(b"1\nH\nH 0 0 0\n").hexdigest(),
        "size_bytes": 12,
    }
    with pytest.raises(ValueError):
        _capture.capture(path, max_bytes=4)


def test_a_bound_input_over_one_source_budget_keeps_its_completion_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Binding and claim accept a bound input up to the generation budget; the
    # completion decision reads the same input under the same budget, so an
    # Opt Freq job is still verified as one.
    from orca_auto.orca import input_syntax
    from orca_auto.orca.completion_rules import detect_completion_mode
    from orca_auto.orca.out_analyzer import analyze_output

    monkeypatch.setattr(_capture, "MAX_INPUT_SNAPSHOT_BYTES", 240)
    monkeypatch.setattr(_capture, "MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES", 960)
    monkeypatch.setattr(input_syntax, "MAX_AGGREGATE_SNAPSHOT_BYTES", 960)
    job_dir, selected, executable = _job(tmp_path)
    selected.write_text(
        "! HF STO-3G Opt Freq\n#" + "x" * 200 + "\n* xyzfile 0 1 h2.xyz\n", encoding="utf-8"
    )
    snapshot = _build(job_dir, selected, executable)
    _verify(job_dir, snapshot)
    bound = Path(snapshot["selected_inp"])
    assert bound.stat().st_size > 240

    mode = detect_completion_mode(bound)

    assert (mode.kind, mode.require_frequency) == ("opt", True)
    out = tmp_path / "job.out"
    out.write_text(
        "FINAL SINGLE POINT ENERGY -1.0\nTHE OPTIMIZATION HAS CONVERGED\n"
        "****ORCA TERMINATED NORMALLY****\n",
        encoding="utf-8",
    )
    analysis = analyze_output(out, mode)
    assert (analysis.status, analysis.reason) == ("incomplete", "frequency_evidence_missing")


@pytest.mark.parametrize("kind", ["missing", "fifo", "over-budget"])
def test_completion_requirements_of_an_unreadable_input_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Unreadable is not empty: the job is never verified as a single point.
    import os

    from orca_auto.orca import input_syntax
    from orca_auto.orca.completion_rules import (
        CompletionInputUnreadableError,
        detect_completion_mode,
    )

    inp = tmp_path / "job.inp"
    if kind == "fifo":
        os.mkfifo(inp)
    elif kind == "over-budget":
        inp.write_text("! HF Opt Freq\n" + "#" * 100 + "\n", encoding="utf-8")
        monkeypatch.setattr(input_syntax, "MAX_AGGREGATE_SNAPSHOT_BYTES", 50)

    with pytest.raises(CompletionInputUnreadableError):
        detect_completion_mode(inp)
