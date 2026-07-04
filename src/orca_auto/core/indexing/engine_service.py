from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine_adapters import EngineLocationArtifacts, EngineLocationRoots, EngineLocationStore
from .engine_records import EngineLocationSpec, build_engine_job_location_record
from .location import JobLocationRecord
from .roots import resolve_latest_job_dir
from .store import get_job_location, list_job_locations, resolve_job_location, upsert_job_location


@dataclass(frozen=True)
class EngineLocationService:
    engine: str
    spec: EngineLocationSpec
    load_state_fn: Callable[[Path], dict[str, Any] | None]
    load_report_json_fn: Callable[[Path], dict[str, Any] | None]
    get_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None] = get_job_location
    list_job_locations_fn: Callable[[str | Path], list[JobLocationRecord]] = list_job_locations
    resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None] = (
        resolve_job_location
    )
    upsert_job_location_fn: Callable[[str | Path, JobLocationRecord], JobLocationRecord] = (
        upsert_job_location
    )

    @property
    def roots(self) -> EngineLocationRoots:
        return EngineLocationRoots(engine=self.engine)

    @property
    def store(self) -> EngineLocationStore:
        return EngineLocationStore(
            get_job_location_fn=self.get_job_location_fn,
            list_job_locations_fn=self.list_job_locations_fn,
            resolve_job_location_fn=self.resolve_job_location_fn,
            upsert_job_location_fn=self.upsert_job_location_fn,
        )

    @property
    def artifacts(self) -> EngineLocationArtifacts:
        return EngineLocationArtifacts(
            spec=self.spec,
            load_state_fn=self.load_state_fn,
            load_report_json_fn=self.load_report_json_fn,
        )

    def index_root_for_cfg(self, cfg: Any) -> Path:
        return self.roots.index_root_for_cfg(cfg)

    def runtime_roots_for_cfg(self, cfg: Any) -> tuple[Path, ...]:
        return self.roots.runtime_roots_for_cfg(cfg)

    def index_root_for_path(self, cfg: Any, *paths: str | Path | None) -> Path:
        return self.roots.index_root_for_path(cfg, *paths)

    def lookup_roots_for_target(self, cfg: Any, target: str) -> tuple[Path, ...]:
        return self.roots.lookup_roots_for_target(cfg, target)

    def list_job_records_for_cfg(self, cfg: Any) -> list[tuple[Path, JobLocationRecord]]:
        return self.roots.list_job_records_for_cfg(
            cfg,
            list_job_locations_fn=self.list_job_locations_fn,
        )

    def resolve_job_location_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, JobLocationRecord | None]:
        return self.roots.resolve_job_location_for_cfg(
            cfg,
            target,
            resolve_job_location_fn=self.resolve_job_location_fn,
        )

    def build_job_location_record(
        self,
        *,
        existing: JobLocationRecord | None = None,
        job_id: str,
        status: str,
        job_dir: Path,
        payload_kind: str,
        selected_input_xyz: str,
        molecule_key: str = "",
        resource_request: dict[str, int] | None = None,
        resource_actual: dict[str, int] | None = None,
    ) -> JobLocationRecord:
        return build_engine_job_location_record(
            spec=self.spec,
            existing=existing,
            job_id=job_id,
            status=status,
            job_dir=job_dir,
            payload_kind=payload_kind,
            selected_input_xyz=selected_input_xyz,
            molecule_key=molecule_key,
            resource_request=resource_request,
            resource_actual=resource_actual,
        )

    def upsert_job_record(
        self,
        cfg: Any,
        *,
        job_id: str,
        status: str,
        job_dir: Path,
        payload_kind: str,
        selected_input_xyz: str,
        molecule_key: str = "",
        resource_request: dict[str, int] | None = None,
        resource_actual: dict[str, int] | None = None,
    ) -> JobLocationRecord:
        root = self.index_root_for_path(cfg, job_dir)
        existing = self.store.existing(root, job_id)
        record = self.build_job_location_record(
            existing=existing,
            job_id=job_id,
            status=status,
            job_dir=job_dir,
            payload_kind=payload_kind,
            selected_input_xyz=selected_input_xyz,
            molecule_key=molecule_key,
            resource_request=resource_request,
            resource_actual=resource_actual,
        )
        return self.store.upsert(root, record)

    def resolve_latest_job_dir(self, index_root: str | Path, target: str) -> Path | None:
        return resolve_latest_job_dir(
            index_root,
            target,
            resolve_job_location_fn=self.resolve_job_location_fn,
        )

    def load_job_artifacts(
        self,
        index_root: str | Path,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        return self.artifacts.load_job_artifacts(
            index_root,
            target,
            resolve_latest_job_dir_fn=self.resolve_latest_job_dir,
        )

    def load_job_artifacts_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
        return self.artifacts.load_job_artifacts_for_cfg(
            cfg,
            target,
            engine=self.engine,
            resolve_latest_job_dir_fn=self.resolve_latest_job_dir,
            resolve_job_location_fn=self.resolve_job_location_fn,
        )

    def record_from_artifacts(
        self,
        *,
        job_dir: Path,
        state: dict[str, Any] | None,
        report: dict[str, Any] | None,
        existing: JobLocationRecord | None = None,
        default_payload_kind: str | None = None,
    ) -> JobLocationRecord | None:
        return self.artifacts.record_from_artifacts(
            build_record_fn=self.build_job_location_record,
            job_dir=job_dir,
            state=state,
            report=report,
            existing=existing,
            default_payload_kind=default_payload_kind,
        )

    def collect_reindex_payload(self, job_dir: Path) -> dict[str, Any] | None:
        return self.artifacts.collect_reindex_payload(job_dir)


__all__ = ["EngineLocationService"]
