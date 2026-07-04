from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine_records import EngineLocationRecordRequest
from .engine_service import EngineLocationService
from .location import JobLocationRecord
from .roots import (
    list_job_records_for_cfg,
    load_job_artifacts,
    load_job_artifacts_for_cfg,
    resolve_job_location_for_cfg,
    resolve_latest_job_dir,
)


@dataclass(frozen=True)
class EngineLocationModule:
    """Small adapter for engine modules that expose job-location helpers.

    xTB and CREST expose module-level functions for their engine-specific
    commands. This object centralizes the repeated delegation while each
    module remains free to pass monkeypatchable store
    functions such as ``resolve_job_location`` at call time.
    """

    service: EngineLocationService
    payload_kind_kwarg: str
    molecule_key_kwarg: str
    default_payload_kind_kwarg: str

    def record_request(self, kwargs: dict[str, Any]) -> EngineLocationRecordRequest:
        return EngineLocationRecordRequest(
            existing=kwargs.get("existing"),
            job_id=kwargs["job_id"],
            status=kwargs["status"],
            job_dir=kwargs["job_dir"],
            payload_kind=kwargs[self.payload_kind_kwarg],
            selected_input_xyz=kwargs["selected_input_xyz"],
            molecule_key=kwargs.get(self.molecule_key_kwarg, ""),
            resource_request=kwargs.get("resource_request"),
            resource_actual=kwargs.get("resource_actual"),
        )

    def build_job_location_record(self, **kwargs: Any) -> JobLocationRecord:
        return self.build_job_location_record_from_request(self.record_request(kwargs))

    def build_job_location_record_from_request(
        self,
        request: EngineLocationRecordRequest,
    ) -> JobLocationRecord:
        return self.service.build_job_location_record(
            existing=request.existing,
            job_id=request.job_id,
            status=request.status,
            job_dir=request.job_dir,
            payload_kind=request.payload_kind,
            selected_input_xyz=request.selected_input_xyz,
            molecule_key=request.molecule_key,
            resource_request=request.resource_request,
            resource_actual=request.resource_actual,
        )

    def upsert_job_record(
        self,
        cfg: Any,
        *,
        get_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
        upsert_job_location_fn: Callable[[str | Path, JobLocationRecord], JobLocationRecord],
        **kwargs: Any,
    ) -> JobLocationRecord:
        request = self.record_request(kwargs)
        root = self.service.index_root_for_path(cfg, request.job_dir)
        existing = get_job_location_fn(root, request.job_id)
        record = self.build_job_location_record_from_request(request.with_existing(existing))
        return upsert_job_location_fn(root, record)

    def list_job_records_for_cfg(
        self,
        cfg: Any,
        *,
        list_job_locations_fn: Callable[[str | Path], list[JobLocationRecord]],
    ) -> list[tuple[Path, JobLocationRecord]]:
        return list_job_records_for_cfg(
            cfg,
            engine=self.service.engine,
            list_job_locations_fn=list_job_locations_fn,
        )

    def resolve_job_location_for_cfg(
        self,
        cfg: Any,
        target: str,
        *,
        resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
    ) -> tuple[Path | None, JobLocationRecord | None]:
        return resolve_job_location_for_cfg(
            cfg,
            target,
            engine=self.service.engine,
            resolve_job_location_fn=resolve_job_location_fn,
        )

    def resolve_latest_job_dir(
        self,
        index_root: str | Path,
        target: str,
        *,
        resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
    ) -> Path | None:
        return resolve_latest_job_dir(
            index_root,
            target,
            resolve_job_location_fn=resolve_job_location_fn,
        )

    def load_job_artifacts(
        self,
        index_root: str | Path,
        target: str,
        *,
        load_state_fn: Callable[[Path], dict[str, Any] | None],
        load_report_json_fn: Callable[[Path], dict[str, Any] | None],
        resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        return load_job_artifacts(
            index_root,
            target,
            load_state_fn=load_state_fn,
            load_report_json_fn=load_report_json_fn,
            resolve_latest_job_dir_fn=lambda root, lookup_target: self.resolve_latest_job_dir(
                root,
                lookup_target,
                resolve_job_location_fn=resolve_job_location_fn,
            ),
        )

    def load_job_artifacts_for_cfg(
        self,
        cfg: Any,
        target: str,
        *,
        load_state_fn: Callable[[Path], dict[str, Any] | None],
        load_report_json_fn: Callable[[Path], dict[str, Any] | None],
        resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
        return load_job_artifacts_for_cfg(
            cfg,
            target,
            engine=self.service.engine,
            load_state_fn=load_state_fn,
            load_report_json_fn=load_report_json_fn,
            resolve_latest_job_dir_fn=lambda root, lookup_target: self.resolve_latest_job_dir(
                root,
                lookup_target,
                resolve_job_location_fn=resolve_job_location_fn,
            ),
            resolve_job_location_fn=resolve_job_location_fn,
        )

    def record_from_artifacts(
        self,
        *,
        job_dir: Path,
        state: dict[str, Any] | None,
        report: dict[str, Any] | None,
        existing: JobLocationRecord | None = None,
        **kwargs: Any,
    ) -> JobLocationRecord | None:
        return self.service.record_from_artifacts(
            job_dir=job_dir,
            state=state,
            report=report,
            existing=existing,
            default_payload_kind=kwargs.get(self.default_payload_kind_kwarg),
        )


__all__ = ["EngineLocationModule"]
