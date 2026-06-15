from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orca_auto.core.utils import normalize_text

from .internal_engine_cancellation import cancel_engine_target
from .internal_engine_models import InternalEngineSubmitterDeps, InternalEngineSubmitterSpec
from .internal_engine_submission import submit_engine_job_dir


@dataclass(frozen=True)
class InternalEngineSubmitter:
    spec: InternalEngineSubmitterSpec
    deps_factory: Callable[[], InternalEngineSubmitterDeps]

    def submit_job_dir(
        self,
        *,
        job_dir: str,
        priority: int,
        config_path: str,
    ) -> dict[str, Any]:
        return submit_engine_job_dir(
            spec=self.spec,
            deps=self.deps_factory(),
            config_path=config_path,
            job_dir=job_dir,
            priority=priority,
        )

    def cancel_target(
        self,
        *,
        target: str,
        config_path: str,
    ) -> dict[str, Any]:
        return cancel_engine_target(
            spec=self.spec,
            deps=self.deps_factory(),
            config_path=config_path,
            target=target,
        )


def build_internal_engine_submitter(
    *,
    run_dir_api_name: str,
    cancel_api_name: str,
    deps_factory: Callable[[], InternalEngineSubmitterDeps],
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None = None,
) -> tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]:
    submitter = InternalEngineSubmitter(
        spec=InternalEngineSubmitterSpec(
            run_dir_api_name=run_dir_api_name,
            cancel_api_name=cancel_api_name,
            extra_fields_fn=extra_fields_fn,
        ),
        deps_factory=deps_factory,
    )
    return submitter.submit_job_dir, submitter.cancel_target


def build_internal_engine_module_submitter(
    *,
    engine: str,
    deps_factory: Callable[[], InternalEngineSubmitterDeps],
    extra_fields_fn: Callable[[Any | None, Any | None], dict[str, Any]] | None = None,
) -> tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]:
    engine_name = normalize_text(engine)
    if not engine_name:
        raise ValueError("build_internal_engine_module_submitter requires engine")
    return build_internal_engine_submitter(
        run_dir_api_name=f"orca_auto.{engine_name}.submission.direct_enqueue",
        cancel_api_name=f"orca_auto.{engine_name}.queue_runtime.direct_cancel",
        deps_factory=deps_factory,
        extra_fields_fn=extra_fields_fn,
    )
