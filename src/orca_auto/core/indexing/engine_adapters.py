from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import engine_artifacts as _engine_artifacts
from .engine_records import EngineLocationSpec
from .location import JobLocationRecord
from .roots import (
    index_root_for_cfg,
    index_root_for_path,
    list_job_records_for_cfg,
    load_job_artifacts,
    load_job_artifacts_for_cfg,
    lookup_roots_for_target,
    resolve_job_location_for_cfg,
    runtime_roots_for_cfg,
)


@dataclass(frozen=True)
class EngineLocationRoots:
    engine: str

    def index_root_for_cfg(self, cfg: Any) -> Path:
        return index_root_for_cfg(cfg)

    def runtime_roots_for_cfg(self, cfg: Any) -> tuple[Path, ...]:
        return runtime_roots_for_cfg(cfg, engine=self.engine)

    def index_root_for_path(self, cfg: Any, *paths: str | Path | None) -> Path:
        return index_root_for_path(cfg, *paths, engine=self.engine)

    def lookup_roots_for_target(self, cfg: Any, target: str) -> tuple[Path, ...]:
        return lookup_roots_for_target(cfg, target, engine=self.engine)

    def list_job_records_for_cfg(
        self,
        cfg: Any,
        *,
        list_job_locations_fn: Callable[[str | Path], list[JobLocationRecord]],
    ) -> list[tuple[Path, JobLocationRecord]]:
        return list_job_records_for_cfg(
            cfg,
            engine=self.engine,
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
            engine=self.engine,
            resolve_job_location_fn=resolve_job_location_fn,
        )


@dataclass(frozen=True)
class EngineLocationStore:
    get_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None]
    list_job_locations_fn: Callable[[str | Path], list[JobLocationRecord]]
    resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None]
    upsert_job_location_fn: Callable[[str | Path, JobLocationRecord], JobLocationRecord]

    def existing(self, root: str | Path, job_id: str) -> JobLocationRecord | None:
        return self.get_job_location_fn(root, job_id)

    def upsert(self, root: str | Path, record: JobLocationRecord) -> JobLocationRecord:
        return self.upsert_job_location_fn(root, record)


@dataclass(frozen=True)
class EngineLocationArtifacts:
    spec: EngineLocationSpec
    load_state_fn: Callable[[Path], dict[str, Any] | None]
    load_report_json_fn: Callable[[Path], dict[str, Any] | None]

    def load_job_artifacts(
        self,
        index_root: str | Path,
        target: str,
        *,
        resolve_latest_job_dir_fn: Callable[[str | Path, str], Path | None],
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        return load_job_artifacts(
            index_root,
            target,
            load_state_fn=self.load_state_fn,
            load_report_json_fn=self.load_report_json_fn,
            resolve_latest_job_dir_fn=resolve_latest_job_dir_fn,
        )

    def load_job_artifacts_for_cfg(
        self,
        cfg: Any,
        target: str,
        *,
        engine: str,
        resolve_latest_job_dir_fn: Callable[[str | Path, str], Path | None],
        resolve_job_location_fn: Callable[[str | Path, str], JobLocationRecord | None],
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
        return load_job_artifacts_for_cfg(
            cfg,
            target,
            engine=engine,
            load_state_fn=self.load_state_fn,
            load_report_json_fn=self.load_report_json_fn,
            resolve_latest_job_dir_fn=resolve_latest_job_dir_fn,
            resolve_job_location_fn=resolve_job_location_fn,
        )

    def record_from_artifacts(
        self,
        *,
        build_record_fn: Callable[..., JobLocationRecord],
        job_dir: Path,
        state: dict[str, Any] | None,
        report: dict[str, Any] | None,
        existing: JobLocationRecord | None = None,
        default_payload_kind: str | None = None,
    ) -> JobLocationRecord | None:
        return _engine_artifacts.engine_record_from_artifacts(
            spec=self.spec,
            build_record_fn=build_record_fn,
            job_dir=job_dir,
            state=state,
            report=report,
            existing=existing,
            default_payload_kind=default_payload_kind,
        )

    def collect_reindex_payload(self, job_dir: Path) -> dict[str, Any] | None:
        resolved_job_dir = job_dir.expanduser().resolve()
        return _engine_artifacts.collect_engine_reindex_payload(
            spec=self.spec,
            job_dir=resolved_job_dir,
            state=self.load_state_fn(resolved_job_dir),
            report=self.load_report_json_fn(resolved_job_dir),
        )


__all__ = [
    "EngineLocationArtifacts",
    "EngineLocationRoots",
    "EngineLocationStore",
]
