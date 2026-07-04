from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import engines as _engine_locations
from .location import JobLocationRecord
from .store import get_job_location, list_job_locations, resolve_job_location, upsert_job_location


@dataclass(frozen=True)
class EngineJobLocationApi:
    """Shared module-level job-location API for engine packages.

    CREST and xTB expose almost identical helpers. This adapter keeps those
    module-level functions thin while preserving monkeypatchable store
    functions through call-time suppliers.
    """

    service: _engine_locations.EngineLocationService
    module: _engine_locations.EngineLocationModule
    get_job_location_fn: Callable[[], Callable[..., Any]]
    list_job_locations_fn: Callable[[], Callable[..., Any]]
    resolve_job_location_fn: Callable[[], Callable[..., Any]]
    upsert_job_location_fn: Callable[[], Callable[..., Any]]
    load_state_fn: Callable[[], Callable[[Path], dict[str, Any] | None]]
    load_report_json_fn: Callable[[], Callable[[Path], dict[str, Any] | None]]

    def list_job_records_for_cfg(self, cfg: Any) -> list[tuple[Path, JobLocationRecord]]:
        return self.module.list_job_records_for_cfg(
            cfg,
            list_job_locations_fn=self.list_job_locations_fn(),
        )

    def resolve_job_location_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, JobLocationRecord | None]:
        return self.module.resolve_job_location_for_cfg(
            cfg,
            target,
            resolve_job_location_fn=self.resolve_job_location_fn(),
        )

    def build_job_location_record(self, **kwargs: Any) -> JobLocationRecord:
        return self.module.build_job_location_record(**kwargs)

    def upsert_job_record(self, cfg: Any, **kwargs: Any) -> JobLocationRecord:
        return self.module.upsert_job_record(
            cfg,
            get_job_location_fn=self.get_job_location_fn(),
            upsert_job_location_fn=self.upsert_job_location_fn(),
            **kwargs,
        )

    def resolve_latest_job_dir(self, index_root: str | Path, target: str) -> Path | None:
        return self.module.resolve_latest_job_dir(
            index_root,
            target,
            resolve_job_location_fn=self.resolve_job_location_fn(),
        )

    def load_job_artifacts(
        self,
        index_root: str | Path,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        return self.module.load_job_artifacts(
            index_root,
            target,
            load_state_fn=self.load_state_fn(),
            load_report_json_fn=self.load_report_json_fn(),
            resolve_job_location_fn=self.resolve_job_location_fn(),
        )

    def load_job_artifacts_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
        return self.module.load_job_artifacts_for_cfg(
            cfg,
            target,
            load_state_fn=self.load_state_fn(),
            load_report_json_fn=self.load_report_json_fn(),
            resolve_job_location_fn=self.resolve_job_location_fn(),
        )

    def record_from_artifacts(self, **kwargs: Any) -> JobLocationRecord | None:
        return self.module.record_from_artifacts(**kwargs)


@dataclass(frozen=True)
class EngineJobLocationApiExports:
    index_root_for_cfg: Callable[..., Any]
    runtime_roots_for_cfg: Callable[..., Any]
    index_root_for_path: Callable[..., Any]
    list_job_records_for_cfg: Callable[..., Any]
    resolve_job_location_for_cfg: Callable[..., Any]
    build_job_location_record: Callable[..., Any]
    upsert_job_record: Callable[..., Any]
    resolve_latest_job_dir: Callable[..., Any]
    load_job_artifacts: Callable[..., Any]
    load_job_artifacts_for_cfg: Callable[..., Any]
    record_from_artifacts: Callable[..., Any]


def engine_job_location_api_exports(api: EngineJobLocationApi) -> EngineJobLocationApiExports:
    return EngineJobLocationApiExports(
        index_root_for_cfg=api.service.index_root_for_cfg,
        runtime_roots_for_cfg=api.service.runtime_roots_for_cfg,
        index_root_for_path=api.service.index_root_for_path,
        list_job_records_for_cfg=api.list_job_records_for_cfg,
        resolve_job_location_for_cfg=api.resolve_job_location_for_cfg,
        build_job_location_record=api.build_job_location_record,
        upsert_job_record=api.upsert_job_record,
        resolve_latest_job_dir=api.resolve_latest_job_dir,
        load_job_artifacts=api.load_job_artifacts,
        load_job_artifacts_for_cfg=api.load_job_artifacts_for_cfg,
        record_from_artifacts=api.record_from_artifacts,
    )


def build_engine_job_location_api(
    *,
    engine: str,
    spec: _engine_locations.EngineLocationSpec,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None],
    payload_kind_kwarg: str,
    molecule_key_kwarg: str,
    default_payload_kind_kwarg: str,
    get_job_location_fn: Callable[[], Callable[..., Any]],
    list_job_locations_fn: Callable[[], Callable[..., Any]],
    resolve_job_location_fn: Callable[[], Callable[..., Any]],
    upsert_job_location_fn: Callable[[], Callable[..., Any]],
    load_state_supplier: Callable[[], Callable[[Path], dict[str, Any] | None]],
    load_report_json_supplier: Callable[[], Callable[[Path], dict[str, Any] | None]],
) -> EngineJobLocationApi:
    service = _engine_locations.EngineLocationService(
        engine=engine,
        spec=spec,
        load_state_fn=load_state_fn,
        load_report_json_fn=load_report_json_fn,
    )
    module = _engine_locations.EngineLocationModule(
        service=service,
        payload_kind_kwarg=payload_kind_kwarg,
        molecule_key_kwarg=molecule_key_kwarg,
        default_payload_kind_kwarg=default_payload_kind_kwarg,
    )
    return EngineJobLocationApi(
        service=service,
        module=module,
        get_job_location_fn=get_job_location_fn,
        list_job_locations_fn=list_job_locations_fn,
        resolve_job_location_fn=resolve_job_location_fn,
        upsert_job_location_fn=upsert_job_location_fn,
        load_state_fn=load_state_supplier,
        load_report_json_fn=load_report_json_supplier,
    )


def build_store_backed_engine_job_location_api(
    *,
    engine: str,
    spec: _engine_locations.EngineLocationSpec,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None],
    payload_kind_kwarg: str,
    molecule_key_kwarg: str,
    default_payload_kind_kwarg: str,
) -> EngineJobLocationApi:
    return build_engine_job_location_api(
        engine=engine,
        spec=spec,
        load_state_fn=load_state_fn,
        load_report_json_fn=load_report_json_fn,
        payload_kind_kwarg=payload_kind_kwarg,
        molecule_key_kwarg=molecule_key_kwarg,
        default_payload_kind_kwarg=default_payload_kind_kwarg,
        get_job_location_fn=lambda: get_job_location,
        list_job_locations_fn=lambda: list_job_locations,
        resolve_job_location_fn=lambda: resolve_job_location,
        upsert_job_location_fn=lambda: upsert_job_location,
        load_state_supplier=lambda: load_state_fn,
        load_report_json_supplier=lambda: load_report_json_fn,
    )


def build_store_backed_engine_job_location_exports(
    *,
    engine: str,
    spec: _engine_locations.EngineLocationSpec,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None],
    payload_kind_kwarg: str,
    molecule_key_kwarg: str,
    default_payload_kind_kwarg: str,
) -> EngineJobLocationApiExports:
    return engine_job_location_api_exports(
        build_store_backed_engine_job_location_api(
            engine=engine,
            spec=spec,
            load_state_fn=load_state_fn,
            load_report_json_fn=load_report_json_fn,
            payload_kind_kwarg=payload_kind_kwarg,
            molecule_key_kwarg=molecule_key_kwarg,
            default_payload_kind_kwarg=default_payload_kind_kwarg,
        )
    )


__all__ = [
    "EngineJobLocationApi",
    "EngineJobLocationApiExports",
    "build_engine_job_location_api",
    "build_store_backed_engine_job_location_api",
    "build_store_backed_engine_job_location_exports",
    "engine_job_location_api_exports",
]
