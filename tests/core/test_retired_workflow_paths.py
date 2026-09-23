from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.core.paths.retired import path_is_retired_workflow_owned


@pytest.mark.parametrize("marker", ["flow.yaml", "workflow.json"])
@pytest.mark.parametrize("location", ["root", "parent", "direct"])
@pytest.mark.parametrize("symlink", [False, True])
def test_retired_markers_protect_owned_descendants(
    tmp_path: Path, marker: str, location: str, symlink: bool
) -> None:
    job = tmp_path / "workspace" / "generation" / "job"
    job.mkdir(parents=True)
    directory = {"root": tmp_path, "parent": job.parent, "direct": job}[location]
    evidence = directory / marker
    if symlink:
        evidence.symlink_to("missing-evidence")
    else:
        evidence.write_text("unreadable or obsolete format", encoding="utf-8")
    assert path_is_retired_workflow_owned(job, tmp_path)
    assert path_is_retired_workflow_owned(job / "input.inp", tmp_path)


def test_ordinary_orca_and_unrelated_workflow_siblings_are_not_owned(tmp_path: Path) -> None:
    ordinary = tmp_path / "orca"
    ordinary.mkdir()
    retired = tmp_path / "retired"
    retired.mkdir()
    (retired / "workflow.json").write_text("{}", encoding="utf-8")
    assert not path_is_retired_workflow_owned(ordinary, tmp_path)
    assert not path_is_retired_workflow_owned(tmp_path.parent / "outside", tmp_path)


def test_retired_marker_applies_to_fd_pinned_alias(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "flow.yaml").write_text("{}", encoding="utf-8")
    descriptor = os.open(job, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert path_is_retired_workflow_owned(Path(f"/proc/self/fd/{descriptor}"), tmp_path)
    finally:
        os.close(descriptor)


def test_marker_inspection_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.lstat

    def denied(path: Path):
        if path.name == "workflow.json":
            raise PermissionError("cannot inspect ownership")
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied)
    assert path_is_retired_workflow_owned(tmp_path / "job", tmp_path)
