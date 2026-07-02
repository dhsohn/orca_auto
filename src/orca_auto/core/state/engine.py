from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
)
from orca_auto.core.utils import (
    atomic_write_json,
    load_json_mapping_file,
    mapping_or_empty,
    normalize_text,
    now_utc_iso,
)
from orca_auto.core.utils import (
    coerce_list as _shared_coerce_list,
)

RECOVERY_PENDING_REASONS = frozenset({"worker_shutdown", "crashed_recovery"})


def coerce_dict(value: Any) -> dict[str, Any]:
    return dict(mapping_or_empty(value))


def coerce_list(value: Any) -> list[Any]:
    return _shared_coerce_list(value)


def write_json_artifact(job_dir: Path, filename: str, payload: dict[str, Any]) -> Path:
    path = job_dir / filename
    atomic_write_json(path, payload, ensure_ascii=True, indent=2)
    return path


def write_text_artifact(job_dir: Path, filename: str, lines: list[str]) -> Path:
    path = job_dir / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_json_mapping_artifact(job_dir: Path, filename: str) -> dict[str, Any] | None:
    return load_json_mapping_file(job_dir / filename)


@dataclass(frozen=True)
class EngineStateFiles:
    state_file_name: str
    report_json_file_name: str
    report_md_file_name: str
    organized_ref_file_name: str

    def write_state(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return write_json_artifact(job_dir, self.state_file_name, payload)

    def write_report_json(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return write_json_artifact(job_dir, self.report_json_file_name, payload)

    def write_report_md_lines(self, job_dir: Path, lines: list[str]) -> Path:
        return write_text_artifact(job_dir, self.report_md_file_name, lines)

    def write_organized_ref(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return write_json_artifact(job_dir, self.organized_ref_file_name, payload)

    def load_state(self, job_dir: Path) -> dict[str, Any] | None:
        return load_json_mapping_artifact(job_dir, self.state_file_name)

    def load_report_json(self, job_dir: Path) -> dict[str, Any] | None:
        return load_json_mapping_artifact(job_dir, self.report_json_file_name)

    def load_organized_ref(self, job_dir: Path) -> dict[str, Any] | None:
        return load_json_mapping_artifact(job_dir, self.organized_ref_file_name)


@dataclass(frozen=True)
class EngineStateAccess:
    files: EngineStateFiles
    report_title: str
    selected_input_label: str
    now_fn: Callable[[], str] = now_utc_iso

    def write_state(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return self.files.write_state(job_dir, payload)

    def write_report_json(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return self.files.write_report_json(job_dir, payload)

    def write_report_md(
        self,
        job_dir: Path,
        *,
        job_id: str,
        status: str,
        reason: str,
        selected_input: str,
    ) -> Path:
        lines = [
            f"# {self.report_title}",
            "",
            f"- Job ID: `{job_id}`",
            f"- Status: `{status}`",
            f"- Reason: `{reason}`",
            f"- {self.selected_input_label}: `{selected_input}`",
            f"- Updated At: `{self.now_fn()}`",
        ]
        return self.files.write_report_md_lines(job_dir, lines)

    def write_report_md_lines(self, job_dir: Path, lines: list[str]) -> Path:
        return self.files.write_report_md_lines(job_dir, lines)

    def write_organized_ref(self, job_dir: Path, payload: dict[str, Any]) -> Path:
        return self.files.write_organized_ref(job_dir, payload)

    def load_state(self, job_dir: Path) -> dict[str, Any] | None:
        return self.files.load_state(job_dir)

    def load_report_json(self, job_dir: Path) -> dict[str, Any] | None:
        return self.files.load_report_json(job_dir)

    def load_organized_ref(self, job_dir: Path) -> dict[str, Any] | None:
        return self.files.load_organized_ref(job_dir)


def create_engine_state_access(
    *,
    state_file_name: str,
    report_json_file_name: str,
    report_md_file_name: str,
    organized_ref_file_name: str,
    report_title: str,
    selected_input_label: str,
    now_fn: Callable[[], str] = now_utc_iso,
) -> EngineStateAccess:
    return EngineStateAccess(
        files=EngineStateFiles(
            state_file_name=state_file_name,
            report_json_file_name=report_json_file_name,
            report_md_file_name=report_md_file_name,
            organized_ref_file_name=organized_ref_file_name,
        ),
        report_title=report_title,
        selected_input_label=selected_input_label,
        now_fn=now_fn,
    )


@dataclass(frozen=True)
class EngineStateBindings:
    access: EngineStateAccess
    recovery_pending: EngineRecoveryPendingWriter


def create_engine_state_bindings(
    *,
    state_file_name: str,
    report_json_file_name: str,
    report_md_file_name: str,
    organized_ref_file_name: str,
    manifest_file_name: str,
    report_title: str,
    selected_input_label: str,
    now_fn: Callable[[], str] = now_utc_iso,
) -> EngineStateBindings:
    access = create_engine_state_access(
        state_file_name=state_file_name,
        report_json_file_name=report_json_file_name,
        report_md_file_name=report_md_file_name,
        organized_ref_file_name=organized_ref_file_name,
        report_title=report_title,
        selected_input_label=selected_input_label,
        now_fn=now_fn,
    )
    return EngineStateBindings(
        access=access,
        recovery_pending=EngineRecoveryPendingWriter(
            access=access,
            manifest_filename=manifest_file_name,
            now_fn=now_fn,
        ),
    )


def state_matches_fields(state: dict[str, Any] | None, fields: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    input_payload = coerce_dict(state.get("input"))
    engine_payload = coerce_dict(state.get("engine_payload"))
    for key, value in fields.items():
        if key == "selected_input_xyz":
            candidate = input_payload.get("selected_xyz_path") or input_payload.get("primary_path")
        else:
            candidate = engine_payload.get(key)
        if normalize_text(candidate) != normalize_text(value):
            return False
    return True


def state_matches_job_identity(
    state: dict[str, Any] | None,
    *,
    selected_input_xyz: str | Path,
    identity_fields: Mapping[str, Any],
) -> bool:
    return state_matches_fields(
        state,
        {
            "selected_input_xyz": selected_input_xyz,
            **{str(key): value for key, value in identity_fields.items()},
        },
    )


def state_matches_engine_job(
    state: dict[str, Any] | None,
    *,
    selected_input_xyz: str | Path,
    **identity_fields: Any,
) -> bool:
    return state_matches_job_identity(
        state,
        selected_input_xyz=selected_input_xyz,
        identity_fields=identity_fields,
    )


def is_recovery_pending_state(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    recovery_payload = coerce_dict(state.get("recovery"))
    if bool(recovery_payload.get("pending")):
        return True
    status_payload = coerce_dict(state.get("status"))
    status = normalize_text(status_payload.get("state")).lower()
    reason = normalize_text(status_payload.get("reason"))
    return status == "queued" and reason in RECOVERY_PENDING_REASONS


def manifest_path_from_existing(
    job_dir: Path,
    existing: dict[str, Any],
    *,
    manifest_filename: str,
) -> str:
    artifacts_payload = coerce_dict(existing.get("artifacts"))
    manifest_path = normalize_text(artifacts_payload.get("manifest_path"))
    if manifest_path:
        return manifest_path
    manifest = (job_dir / manifest_filename).resolve()
    return str(manifest) if manifest.exists() else ""


RecoveryFieldMap = Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class RecoveryRetainedFieldsSpec:
    int_fields: tuple[str, ...] = ()
    list_fields: tuple[str, ...] = ()
    dict_fields: tuple[str, ...] = ()


def recovery_identity_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): normalize_text(value) for key, value in fields.items()}


def recovery_identity_payload(
    fields: Mapping[str, Any],
    *,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = recovery_identity_fields(fields)
    if extra_fields:
        payload.update({str(key): value for key, value in extra_fields.items()})
    return payload


def recovery_retained_fields(
    existing: dict[str, Any],
    spec: RecoveryRetainedFieldsSpec,
    *,
    list_fallbacks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fallbacks: Mapping[str, Any] = list_fallbacks or {}
    retained: dict[str, Any] = {}
    engine_payload = coerce_dict(existing.get("engine_payload"))
    for field in spec.int_fields:
        retained[field] = int(engine_payload.get(field, 0) or 0)
    for field in spec.list_fields:
        values = coerce_list(engine_payload.get(field))
        if not values and field in fallbacks:
            values = coerce_list(fallbacks[field])
        retained[field] = values
    for field in spec.dict_fields:
        retained[field] = coerce_dict(engine_payload.get(field))
    return retained


def _resolve_recovery_fields(
    fields: RecoveryFieldMap | None,
    existing: dict[str, Any],
) -> dict[str, Any]:
    if fields is None:
        return {}
    resolved = fields(existing) if callable(fields) else fields
    return {str(key): value for key, value in resolved.items()}


def recovery_pending_payload(
    job_dir: Path,
    *,
    existing: dict[str, Any],
    job_id: str,
    selected_input_xyz: str | Path,
    reason: str,
    now: str,
    manifest_filename: str,
    identity_fields: dict[str, Any],
    retained_fields: dict[str, Any],
    resource_request: dict[str, Any] | None,
    resource_actual: dict[str, Any] | None,
) -> dict[str, Any]:
    existing_job = coerce_dict(existing.get("job"))
    existing_timestamps = coerce_dict(existing.get("timestamps"))
    existing_recovery = coerce_dict(existing.get("recovery"))
    existing_resources = coerce_dict(existing.get("resources"))
    engine = normalize_text(existing.get("engine"))
    if not engine:
        engine = "xtb" if "job_type" in identity_fields else "crest"
    engine_payload = {
        **{str(key): value for key, value in identity_fields.items()},
        **{str(key): value for key, value in retained_fields.items()},
    }
    return build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(
            id=normalize_text(existing_job.get("id")) or normalize_text(job_id),
            queue_id=normalize_text(existing_job.get("queue_id")),
            dir=str(job_dir.resolve()),
            app_name=normalize_text(existing_job.get("app_name")),
            task_id=normalize_text(existing_job.get("task_id")) or normalize_text(job_id),
        ),
        status=EngineArtifactStatus(state="queued", reason=normalize_text(reason)),
        input=EngineArtifactInput(
            primary_path=normalize_text(selected_input_xyz),
            selected_xyz_path=normalize_text(selected_input_xyz),
        ),
        resources=EngineArtifactResources(
            request=coerce_dict(resource_request) or coerce_dict(existing_resources.get("request")),
            actual=coerce_dict(resource_actual) or coerce_dict(existing_resources.get("actual")),
        ),
        timestamps=EngineArtifactTimestamps(
            created_at=normalize_text(existing_timestamps.get("created_at")) or now,
            started_at=normalize_text(existing_timestamps.get("started_at")),
            updated_at=now,
        ),
        recovery=EngineArtifactRecovery(
            pending=True,
            reason=normalize_text(reason),
            count=int(existing_recovery.get("count", 0) or 0) + 1,
            resumed=False,
        ),
        artifacts={
            "manifest_path": manifest_path_from_existing(
                job_dir,
                existing,
                manifest_filename=manifest_filename,
            ),
            "stdout_log": "",
            "stderr_log": "",
            "organized_dir": "",
        },
        engine_payload=engine_payload,
    )


@dataclass(frozen=True)
class EngineRecoveryPendingWriter:
    access: EngineStateAccess
    manifest_filename: str
    now_fn: Callable[[], str] = now_utc_iso

    def write(
        self,
        job_dir: Path,
        *,
        job_id: str,
        selected_input_xyz: str | Path,
        reason: str,
        identity_fields: RecoveryFieldMap,
        resource_request: dict[str, Any] | None,
        resource_actual: dict[str, Any] | None,
        retained_fields: RecoveryFieldMap | None = None,
    ) -> dict[str, Any]:
        existing = self.access.load_state(job_dir) or {}
        payload = recovery_pending_payload(
            job_dir,
            existing=existing,
            job_id=job_id,
            selected_input_xyz=selected_input_xyz,
            reason=reason,
            now=self.now_fn(),
            manifest_filename=self.manifest_filename,
            identity_fields=_resolve_recovery_fields(identity_fields, existing),
            retained_fields=_resolve_recovery_fields(retained_fields, existing),
            resource_request=resource_request,
            resource_actual=resource_actual,
        )
        self.access.write_state(job_dir, payload)
        return payload


def write_recovery_pending_state(
    recovery_pending: EngineRecoveryPendingWriter,
    job_dir: Path,
    *,
    job_id: str,
    selected_input_xyz: str | Path,
    reason: str,
    identity_fields: RecoveryFieldMap,
    resource_request: dict[str, Any] | None,
    resource_actual: dict[str, Any] | None,
    retained_fields: RecoveryFieldMap | None = None,
) -> dict[str, Any]:
    return recovery_pending.write(
        job_dir,
        job_id=job_id,
        selected_input_xyz=selected_input_xyz,
        reason=reason,
        identity_fields=identity_fields,
        retained_fields=retained_fields,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )


@dataclass(frozen=True)
class EngineStateModuleExports:
    access: EngineStateAccess
    recovery_pending: EngineRecoveryPendingWriter
    write_state: Callable[[Path, dict[str, Any]], Path]
    write_report_json: Callable[[Path, dict[str, Any]], Path]
    write_report_md_lines: Callable[[Path, list[str]], Path]
    write_organized_ref: Callable[[Path, dict[str, Any]], Path]
    load_state: Callable[[Path], dict[str, Any] | None]
    load_report_json: Callable[[Path], dict[str, Any] | None]
    load_organized_ref: Callable[[Path], dict[str, Any] | None]
    write_report_md: Callable[..., Path]


@dataclass(frozen=True)
class EngineStateModuleSpec:
    state_file_name: str
    report_json_file_name: str
    report_md_file_name: str
    organized_ref_file_name: str
    manifest_file_name: str
    report_title: str
    selected_input_label: str


def engine_state_module_exports(bindings: EngineStateBindings) -> EngineStateModuleExports:
    access = bindings.access
    return EngineStateModuleExports(
        access=access,
        recovery_pending=bindings.recovery_pending,
        write_state=access.write_state,
        write_report_json=access.write_report_json,
        write_report_md_lines=access.write_report_md_lines,
        write_organized_ref=access.write_organized_ref,
        load_state=access.load_state,
        load_report_json=access.load_report_json,
        load_organized_ref=access.load_organized_ref,
        write_report_md=access.write_report_md,
    )


def create_engine_state_module_exports(
    spec: EngineStateModuleSpec,
    *,
    now_fn: Callable[[], str] = now_utc_iso,
) -> EngineStateModuleExports:
    return engine_state_module_exports(
        create_engine_state_bindings(
            state_file_name=spec.state_file_name,
            report_json_file_name=spec.report_json_file_name,
            report_md_file_name=spec.report_md_file_name,
            organized_ref_file_name=spec.organized_ref_file_name,
            manifest_file_name=spec.manifest_file_name,
            report_title=spec.report_title,
            selected_input_label=spec.selected_input_label,
            now_fn=now_fn,
        )
    )
