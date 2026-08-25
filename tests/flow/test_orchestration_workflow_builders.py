from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from orca_auto.core.commands.run_dir import use_run_dir_publication_guard
from orca_auto.flow.contracts import (
    WorkflowArtifactRef,
    WorkflowStagePayload,
    WorkflowTemplateRequest,
)
from orca_auto.flow.orchestration import workflow_builders as workflow_builder_module
from orca_auto.flow.orchestration.requests import (
    ReactionTsSearchWorkflowRequest,
    WorkflowCreationContext,
    WorkflowPersistenceContext,
)
from orca_auto.flow.orchestration.workflow_builders import (
    _copy_reaction_inputs,
    _merge_manifest_defaults,
    _persist_workflow,
    _ReactionWorkflowInputs,
    _workflow_workspace,
    _WorkflowWorkspace,
)
from orca_auto.flow.workflow.store import WORKFLOW_CREATION_MARKER_FILE


def _workflow_context(
    *,
    copy_input_fn: Any | None = None,
    workflow_id_factory: Any | None = None,
    write_workflow_payload_fn: Any | None = None,
    sync_workflow_registry_fn: Any | None = None,
) -> WorkflowCreationContext:
    return WorkflowCreationContext(
        workflow_id_factory=workflow_id_factory or (lambda: "wf_demo_generated"),
        copy_input_fn=copy_input_fn or (lambda source, target: str(target)),
        now_utc_iso_fn=lambda: "2026-05-29T00:00:00+00:00",
        new_crest_stage_fn=lambda **_kwargs: cast(Any, {}),
        write_workflow_payload_fn=write_workflow_payload_fn
        or (lambda _workspace_dir, _payload: None),
        sync_workflow_registry_fn=sync_workflow_registry_fn
        or (lambda _root, _workspace_dir, _payload: None),
    )


def test_merge_manifest_defaults_trims_keys_and_removes_blank_overrides() -> None:
    assert _merge_manifest_defaults(
        {"rthr": 0.3, "keep": "yes"},
        {
            " rthr ": 0.5,
            "keep": "",
            "drop": None,
            "   ": "ignored",
        },
    ) == {"rthr": 0.5}


def test_workflow_workspace_generates_id_and_requested_at(tmp_path: Path) -> None:
    workspace = _workflow_workspace(
        workflow_id="   ",
        workflow_root=tmp_path / "workflows",
        workspace_parent=None,
        context=_workflow_context(),
    )

    assert workspace.workflow_id == "wf_demo_generated"
    assert workspace.workflow_root_path == (tmp_path / "workflows").resolve()
    assert workspace.workspace_dir == (tmp_path / "workflows" / "wf_demo_generated").resolve()
    assert workspace.requested_at == "2026-05-29T00:00:00+00:00"


@pytest.mark.parametrize(
    "workflow_id",
    [
        "../outside",
        "nested/wf",
        "nested\\wf",
        ".",
        "..",
        "/tmp/wf_absolute",
    ],
)
def test_workflow_workspace_rejects_unsafe_workflow_id(
    tmp_path: Path,
    workflow_id: str,
) -> None:
    with pytest.raises(ValueError, match="single path segment"):
        _workflow_workspace(
            workflow_id=workflow_id,
            workflow_root=tmp_path / "workflows",
            workspace_parent=None,
            context=_workflow_context(),
        )


def test_workflow_workspace_rejects_unsafe_generated_workflow_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single path segment"):
        _workflow_workspace(
            workflow_id=None,
            workflow_root=tmp_path / "workflows",
            workspace_parent=None,
            context=_workflow_context(workflow_id_factory=lambda: "../generated"),
        )


@pytest.mark.parametrize("workflow_id", ["TS8(wf)", "TS8(wf", "TS8wf)"])
def test_workflow_workspace_rejects_parenthesized_workflow_id(
    tmp_path: Path,
    workflow_id: str,
) -> None:
    with pytest.raises(ValueError, match="cannot contain parentheses") as error:
        _workflow_workspace(
            workflow_id=workflow_id,
            workflow_root=tmp_path / "workflows",
            workspace_parent=None,
            context=_workflow_context(),
        )

    assert workflow_id in str(error.value)
    assert not (tmp_path / "workflows" / workflow_id).exists()


def test_workflow_workspace_rejects_existing_workflow_payload(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workflows" / "wf_existing"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "workflow.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="workflow already exists"):
        _workflow_workspace(
            workflow_id="wf_existing",
            workflow_root=tmp_path / "workflows",
            workspace_parent=None,
            context=_workflow_context(),
        )


def test_workflow_workspace_rejects_existing_scaffold_without_payload(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workflows" / "rxn.case-01"
    workspace_dir.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="workflow already exists"):
        _workflow_workspace(
            workflow_id="rxn.case-01",
            workflow_root=tmp_path / "workflows",
            workspace_parent=None,
            context=_workflow_context(),
        )


def test_workflow_workspace_reclaims_stale_owned_reservation(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    workspace_dir = workflow_root / "wf_stale"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / WORKFLOW_CREATION_MARKER_FILE).write_text(
        '{"owner_pid": 999999999, "owner_process_start": "stale"}',
        encoding="utf-8",
    )
    (workspace_dir / "stale.txt").write_text("stale", encoding="utf-8")

    workspace = _workflow_workspace(
        workflow_id="wf_stale",
        workflow_root=workflow_root,
        workspace_parent=None,
        context=_workflow_context(),
    )

    assert workspace.workspace_dir == workspace_dir.resolve()
    assert not (workspace_dir / "stale.txt").exists()
    assert (workspace_dir / WORKFLOW_CREATION_MARKER_FILE).is_file()


def test_workflow_workspace_rejects_second_live_reservation(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    _workflow_workspace(
        workflow_id="wf_live",
        workflow_root=workflow_root,
        workspace_parent=None,
        context=_workflow_context(),
    )

    with pytest.raises(FileExistsError, match="workflow already exists"):
        _workflow_workspace(
            workflow_id="wf_live",
            workflow_root=workflow_root,
            workspace_parent=None,
            context=_workflow_context(),
        )


def test_workflow_workspace_cleans_staging_when_durable_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_durable_mkdir = workflow_builder_module.durable_mkdir

    def fail_after_staging_create(path: str | Path, **kwargs: Any) -> Path:
        created = real_durable_mkdir(path, **kwargs)
        if ".creating-" in Path(path).name:
            raise PermissionError("injected directory fsync failure")
        return created

    monkeypatch.setattr(workflow_builder_module, "durable_mkdir", fail_after_staging_create)
    workflow_root = tmp_path / "workflows"

    with pytest.raises(PermissionError, match="injected"):
        _workflow_workspace(
            workflow_id="wf_fsync",
            workflow_root=workflow_root,
            workspace_parent=None,
            context=_workflow_context(),
        )

    assert not (workflow_root / "wf_fsync").exists()
    assert not list(workflow_root.glob(".wf_fsync.creating-*"))


def test_copy_reaction_inputs_uses_role_directories_and_reaction_key(tmp_path: Path) -> None:
    copied: list[tuple[str, Path]] = []

    def copy_input(source: str, target: Path) -> str:
        copied.append((source, target))
        return str(target)

    workspace = _WorkflowWorkspace(
        workflow_id="wf_rxn",
        workflow_root_path=tmp_path,
        workspace_dir=tmp_path / "wf_rxn",
        requested_at="2026-05-29T00:00:00+00:00",
    )

    inputs = _copy_reaction_inputs(
        ReactionTsSearchWorkflowRequest(
            reactant_xyz="/inputs/reactant.xyz",
            product_xyz="/inputs/product.xyz",
            workflow_root=tmp_path,
        ),
        workspace,
        _workflow_context(copy_input_fn=copy_input),
    )

    assert isinstance(inputs, _ReactionWorkflowInputs)
    assert inputs.reaction_key == "reactant_to_product"
    assert copied == [
        ("/inputs/reactant.xyz", tmp_path / "wf_rxn" / "inputs" / "reactants" / "reactant.xyz"),
        ("/inputs/product.xyz", tmp_path / "wf_rxn" / "inputs" / "products" / "product.xyz"),
    ]


def test_persist_workflow_writes_payload_and_syncs_registry(tmp_path: Path) -> None:
    writes: list[tuple[Path, dict[str, Any]]] = []
    syncs: list[tuple[Path, Path, dict[str, Any]]] = []

    def write_payload(workspace_dir: Path, payload: dict[str, Any]) -> None:
        writes.append((workspace_dir, payload))

    def sync_registry(root: Path, workspace_dir: Path, payload: dict[str, Any]) -> None:
        syncs.append((root, workspace_dir, payload))

    workspace_dir = tmp_path / "workflows" / "wf_1"
    request = WorkflowTemplateRequest(
        workflow_id="wf_1",
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        status="planned",
        requested_at="2026-05-29T00:00:00+00:00",
        parameters={"crest_mode": "standard"},
        source_artifacts=(WorkflowArtifactRef(kind="input_xyz", path="/inputs/mol.xyz"),),
    )
    stages = cast(list[WorkflowStagePayload], [{"stage_id": "crest_conformer_01"}])

    payload = _persist_workflow(
        persistence_context=WorkflowPersistenceContext(
            workflow_root_path=tmp_path / "workflows",
            workspace_dir=workspace_dir,
            workflow_id="wf_1",
            template_name="conformer_screening",
            source_job_id="",
            source_job_type="raw_xyz",
            reaction_key="mol",
            requested_at="2026-05-29T00:00:00+00:00",
        ),
        request=request,
        stages=stages,
        creation_context=_workflow_context(
            write_workflow_payload_fn=write_payload,
            sync_workflow_registry_fn=sync_registry,
        ),
        source_inputs=("/inputs/mol.xyz",),
    )

    assert payload["metadata"]["request"]["parameters"] == {"crest_mode": "standard"}
    assert payload["metadata"]["workspace_dir"] == str(workspace_dir)
    assert payload["stages"] == stages
    assert writes == [(workspace_dir, cast(dict[str, Any], payload))]
    assert syncs == [
        (tmp_path / "workflows", workspace_dir, cast(dict[str, Any], payload)),
    ]


def test_public_run_dir_guard_aborts_workflow_before_first_durable_write(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    workspace_dir = workflow_root / "wf_guarded"
    writes: list[dict[str, Any]] = []
    syncs: list[dict[str, Any]] = []
    stages: list[str] = []
    request = WorkflowTemplateRequest(
        workflow_id="wf_guarded",
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        status="planned",
        requested_at="2026-05-29T00:00:00+00:00",
        parameters={},
        source_artifacts=(),
    )

    def reject_publication(stage: str) -> None:
        stages.append(stage)
        raise RuntimeError("run-dir target moved into reserved smoke results")

    with (
        use_run_dir_publication_guard(reject_publication),
        pytest.raises(RuntimeError, match="moved into reserved smoke results"),
    ):
        _persist_workflow(
            persistence_context=WorkflowPersistenceContext(
                workflow_root_path=workflow_root,
                workspace_dir=workspace_dir,
                workflow_id="wf_guarded",
                template_name="conformer_screening",
                source_job_id="",
                source_job_type="raw_xyz",
                reaction_key="mol",
                requested_at="2026-05-29T00:00:00+00:00",
            ),
            request=request,
            stages=[],
            creation_context=_workflow_context(
                write_workflow_payload_fn=lambda _workspace, payload: writes.append(payload),
                sync_workflow_registry_fn=lambda _root, _workspace, payload: syncs.append(payload),
            ),
            source_inputs=("/inputs/mol.xyz",),
        )

    assert stages == ["workflow durable payload pre-commit"]
    assert writes == []
    assert syncs == []
    assert not (workspace_dir / "workflow.json").exists()
    assert not (workflow_root / "workflow_registry.json").exists()


def test_public_run_dir_guard_removes_workflow_payload_after_post_commit_rejection(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    workspace_dir = workflow_root / "wf_guarded"
    workspace_dir.mkdir(parents=True)
    stages: list[str] = []
    syncs: list[dict[str, Any]] = []
    request = WorkflowTemplateRequest(
        workflow_id="wf_guarded",
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        status="planned",
        requested_at="2026-05-29T00:00:00+00:00",
        parameters={},
        source_artifacts=(),
    )

    def reject_after_commit(stage: str) -> None:
        stages.append(stage)
        if stage.endswith("post-commit"):
            raise RuntimeError("run-dir target moved into reserved smoke results")

    def write_payload(workspace: Path, _payload: dict[str, Any]) -> None:
        (workspace / "workflow.json").write_text("{}\n", encoding="utf-8")

    with (
        use_run_dir_publication_guard(reject_after_commit),
        pytest.raises(RuntimeError, match="moved into reserved smoke results"),
    ):
        _persist_workflow(
            persistence_context=WorkflowPersistenceContext(
                workflow_root_path=workflow_root,
                workspace_dir=workspace_dir,
                workflow_id="wf_guarded",
                template_name="conformer_screening",
                source_job_id="",
                source_job_type="raw_xyz",
                reaction_key="mol",
                requested_at="2026-05-29T00:00:00+00:00",
            ),
            request=request,
            stages=[],
            creation_context=_workflow_context(
                write_workflow_payload_fn=write_payload,
                sync_workflow_registry_fn=lambda _root, _workspace, payload: syncs.append(payload),
            ),
            source_inputs=("/inputs/mol.xyz",),
        )

    assert stages == [
        "workflow durable payload pre-commit",
        "workflow durable payload post-commit",
    ]
    assert not (workspace_dir / "workflow.json").exists()
    assert syncs == []


def test_persist_workflow_writes_and_syncs_under_creation_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    @contextmanager
    def creation_lock(_workflow_root: Path):
        events.append("lock_enter")
        try:
            yield
        finally:
            events.append("lock_exit")

    def write_payload(_workspace_dir: Path, _payload: dict[str, Any]) -> None:
        events.append("write")

    def sync_registry(_root: Path, _workspace_dir: Path, _payload: dict[str, Any]) -> None:
        events.append("sync")

    monkeypatch.setattr(workflow_builder_module, "acquire_workflow_create_lock", creation_lock)

    request = WorkflowTemplateRequest(
        workflow_id="wf_lock",
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        status="planned",
        requested_at="2026-05-29T00:00:00+00:00",
        parameters={},
        source_artifacts=(),
    )

    _persist_workflow(
        persistence_context=WorkflowPersistenceContext(
            workflow_root_path=tmp_path / "workflows",
            workspace_dir=tmp_path / "workflows" / "wf_lock",
            workflow_id="wf_lock",
            template_name="conformer_screening",
            source_job_id="",
            source_job_type="raw_xyz",
            reaction_key="mol",
            requested_at="2026-05-29T00:00:00+00:00",
        ),
        request=request,
        stages=[],
        creation_context=_workflow_context(
            write_workflow_payload_fn=write_payload,
            sync_workflow_registry_fn=sync_registry,
        ),
        source_inputs=("/inputs/mol.xyz",),
    )

    assert events == ["write", "sync"]


def test_persist_workflow_rechecks_existing_payload_under_creation_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflows" / "wf_race"
    events: list[str] = []

    @contextmanager
    def creation_lock(_workflow_root: Path):
        events.append("lock_enter")
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "workflow.json").write_text("{}", encoding="utf-8")
        try:
            yield
        finally:
            events.append("lock_exit")

    monkeypatch.setattr(workflow_builder_module, "acquire_workflow_create_lock", creation_lock)
    request = WorkflowTemplateRequest(
        workflow_id="wf_race",
        template_name="conformer_screening",
        source_job_id="",
        source_job_type="raw_xyz",
        reaction_key="mol",
        status="planned",
        requested_at="2026-05-29T00:00:00+00:00",
        parameters={},
        source_artifacts=(),
    )

    _persist_workflow(
        persistence_context=WorkflowPersistenceContext(
            workflow_root_path=tmp_path / "workflows",
            workspace_dir=workspace_dir,
            workflow_id="wf_race",
            template_name="conformer_screening",
            source_job_id="",
            source_job_type="raw_xyz",
            reaction_key="mol",
            requested_at="2026-05-29T00:00:00+00:00",
        ),
        request=request,
        stages=[],
        creation_context=_workflow_context(
            write_workflow_payload_fn=lambda _workspace_dir, _payload: events.append("write"),
            sync_workflow_registry_fn=lambda _root, _workspace_dir, _payload: events.append("sync"),
        ),
        source_inputs=("/inputs/mol.xyz",),
    )

    assert events == ["write", "sync"]
