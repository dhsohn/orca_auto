from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import engine_artifacts as _engine_artifacts
from .engine_records import EngineLocationSpec, build_engine_job_location_record
from .location import JobLocationRecord
from .roots import (
    index_root_for_cfg,
    index_root_for_path,
    list_job_records_for_cfg,
    load_job_artifacts,
    load_job_artifacts_for_cfg,
    resolve_job_location_for_cfg,
    resolve_latest_job_dir,
    runtime_roots_for_cfg,
)
from .store import get_job_location, list_job_locations, resolve_job_location, upsert_job_location


@dataclass(frozen=True)
class EngineJobLocations:
    """Store-backed job-location API shared by the engine packages.

    CREST, xTB, and xTB-MD bind these methods to module-level names. Store
    functions are resolved as globals of this module at call time, so tests
    can monkeypatch e.g. ``engine_job_locations.resolve_job_location`` and the
    already-built engine exports observe the patched function.
    """

    engine: str
    spec: EngineLocationSpec
    load_state_fn: Callable[[Path], dict[str, Any] | None]
    load_report_json_fn: Callable[[Path], dict[str, Any] | None] | None
    payload_kind_kwarg: str
    molecule_key_kwarg: str
    default_payload_kind_kwarg: str

    def index_root_for_cfg(self, cfg: Any) -> Path:
        return index_root_for_cfg(cfg)

    def runtime_roots_for_cfg(self, cfg: Any) -> tuple[Path, ...]:
        return runtime_roots_for_cfg(cfg, engine=self.engine)

    def index_root_for_path(self, cfg: Any, *paths: str | Path | None) -> Path:
        return index_root_for_path(cfg, *paths, engine=self.engine)

    def list_job_records_for_cfg(self, cfg: Any) -> list[tuple[Path, JobLocationRecord]]:
        return list_job_records_for_cfg(
            cfg,
            engine=self.engine,
            list_job_locations_fn=list_job_locations,
        )

    def resolve_job_location_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, JobLocationRecord | None]:
        return resolve_job_location_for_cfg(
            cfg,
            target,
            engine=self.engine,
            resolve_job_location_fn=resolve_job_location,
        )

    def build_job_location_record(self, **kwargs: Any) -> JobLocationRecord:
        """Engine-facing builder taking the engine's kwarg names.

        The payload kind and molecule key arrive under the engine-specific
        names (e.g. ``job_type``/``reaction_key`` for xTB) and are mapped to
        the canonical record fields here.
        """
        return build_engine_job_location_record(
            spec=self.spec,
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

    def _build_canonical_record(self, **kwargs: Any) -> JobLocationRecord:
        return build_engine_job_location_record(spec=self.spec, **kwargs)

    def upsert_job_record(self, cfg: Any, **kwargs: Any) -> JobLocationRecord:
        root = self.index_root_for_path(cfg, kwargs["job_dir"])
        existing = get_job_location(root, kwargs["job_id"])
        record = self.build_job_location_record(**{**kwargs, "existing": existing})
        return upsert_job_location(root, record)

    def resolve_latest_job_dir(self, index_root: str | Path, target: str) -> Path | None:
        return resolve_latest_job_dir(
            index_root,
            target,
            resolve_job_location_fn=resolve_job_location,
        )

    def load_job_artifacts(
        self,
        index_root: str | Path,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
        return load_job_artifacts(
            index_root,
            target,
            load_state_fn=self.load_state_fn,
            load_report_json_fn=self.load_report_json_fn,
            resolve_latest_job_dir_fn=self.resolve_latest_job_dir,
        )

    def load_job_artifacts_for_cfg(
        self,
        cfg: Any,
        target: str,
    ) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
        return load_job_artifacts_for_cfg(
            cfg,
            target,
            engine=self.engine,
            load_state_fn=self.load_state_fn,
            load_report_json_fn=self.load_report_json_fn,
            resolve_latest_job_dir_fn=self.resolve_latest_job_dir,
            resolve_job_location_fn=resolve_job_location,
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
        return _engine_artifacts.engine_record_from_artifacts(
            spec=self.spec,
            build_record_fn=self._build_canonical_record,
            job_dir=job_dir,
            state=state,
            report=report,
            existing=existing,
            default_payload_kind=kwargs.get(self.default_payload_kind_kwarg),
        )


def build_store_backed_engine_job_location_exports(
    *,
    engine: str,
    spec: EngineLocationSpec,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None] | None,
    payload_kind_kwarg: str,
    molecule_key_kwarg: str,
    default_payload_kind_kwarg: str,
) -> EngineJobLocations:
    return EngineJobLocations(
        engine=engine,
        spec=spec,
        load_state_fn=load_state_fn,
        load_report_json_fn=load_report_json_fn,
        payload_kind_kwarg=payload_kind_kwarg,
        molecule_key_kwarg=molecule_key_kwarg,
        default_payload_kind_kwarg=default_payload_kind_kwarg,
    )


__all__ = [
    "EngineJobLocations",
    "build_store_backed_engine_job_location_exports",
]
