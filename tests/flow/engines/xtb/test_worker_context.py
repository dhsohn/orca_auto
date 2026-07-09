from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.flow.engines.xtb import worker_context


def test_default_worker_execution_hooks_resolve_entry_metadata(tmp_path: Path) -> None:
    job_dir = tmp_path / "Screening Batch"
    selected_xyz = job_dir / "input.xyz"
    entry = SimpleNamespace(
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "input_summary": {"candidate_count": 2},
        }
    )

    hooks = worker_context.default_worker_execution_hooks()

    assert hooks.job_dir(entry) == job_dir.resolve()
    assert hooks.selected_xyz(entry) == selected_xyz.resolve()
    assert hooks.job_type(entry) == "path_search"
    assert hooks.reaction_key(entry, job_dir) == "screening_batch"
    assert hooks.input_summary(entry) == {"candidate_count": 2}


def test_build_execution_context_uses_injected_context_dependencies(tmp_path: Path) -> None:
    job_dir = tmp_path / "job-1"
    selected_xyz = job_dir / "candidate.xyz"
    previous_state = {"status": "running"}
    entry = SimpleNamespace(metadata={})
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(tmp_path)),
        resources={},
    )

    context_deps = SimpleNamespace(
        job_dir=lambda _entry: job_dir,
        selected_xyz=lambda _entry: selected_xyz,
        job_type=lambda _entry: "ranking",
        reaction_key=lambda _entry, _job_dir: "rxn-1",
        input_summary=lambda _entry: {"candidate_count": 1},
        entry_resource_request=lambda _cfg, _entry: {"max_cores": 4, "max_memory_gb": 8},
        matching_state=lambda *args, **kwargs: previous_state,
        is_recovery_pending=lambda state: state is previous_state,
    )

    context = worker_context.build_execution_context(
        cfg,
        entry,
        context_deps=context_deps,
    )

    assert context == worker_context.XtbExecutionContext(
        entry=entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        job_type="ranking",
        reaction_key="rxn-1",
        input_summary={"candidate_count": 1},
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        previous_state=previous_state,
        resumed=True,
    )


def test_build_execution_context_rejects_selected_input_outside_job_dir(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    job_dir = allowed_root / "job"
    outside_input = allowed_root / "other" / "input.xyz"
    allowed_root.mkdir()
    entry = SimpleNamespace(metadata={})
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(allowed_root)),
        resources={},
    )
    context_deps = SimpleNamespace(
        job_dir=lambda _entry: job_dir,
        selected_xyz=lambda _entry: outside_input,
        job_type=lambda _entry: "path_search",
        reaction_key=lambda _entry, _job_dir: "rxn-1",
        input_summary=lambda _entry: {},
        entry_resource_request=lambda _cfg, _entry: {},
        matching_state=lambda *args, **kwargs: {},
        is_recovery_pending=lambda _state: False,
    )

    with pytest.raises(ValueError, match="selected_input_xyz.*allowed root"):
        worker_context.build_execution_context(
            cfg,
            entry,
            context_deps=context_deps,
        )
