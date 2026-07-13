from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto import cli_common
from orca_auto.cli_common import (
    _workflow_root_for_args as _cli_workflow_root_for_args,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.flow.orchestration.charge_spin import strict_int

from ..manifest import (
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    optional_positive_float,
    require_crest_candidate_count,
    validate_conformer_postprocessing_template,
    validate_interaction_energy_state_balance,
)

RUN_DIR_COMMON_WORKFLOW_OPTION_FIELDS = (
    "workflow_root",
    "crest_mode",
    "priority",
    "max_cores",
    "max_memory_gb",
    "max_orca_stages",
    "orca_route_line",
    "charge",
    "multiplicity",
)


@dataclass(frozen=True)
class RunDirManifestSections:
    resources: dict[str, Any]
    crest: dict[str, Any]
    xtb: dict[str, Any]
    endpoint_pairing: dict[str, Any]
    orca: dict[str, Any]


@dataclass(frozen=True)
class RunDirWorkflowOptions:
    workflow_root: str
    crest_mode: str
    priority: int
    max_cores: int
    max_memory_gb: int
    max_orca_stages: int
    orca_route_line: str
    charge: int
    multiplicity: int
    max_crest_candidates: int
    max_xtb_stages: int
    boltzmann_temperature_k: float | None = None
    interaction_energy: dict[str, Any] | None = None
    rmsd_dedup: dict[str, Any] | None = None

    def common_kwargs(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in RUN_DIR_COMMON_WORKFLOW_OPTION_FIELDS}


@dataclass(frozen=True)
class RunDirWorkflowConfig:
    workflow_dir: Path
    manifest: dict[str, Any]
    sections: RunDirManifestSections
    reactant_xyz: str
    product_xyz: str
    input_xyz: str
    workflow_type: str

    @property
    def crest_manifest(self) -> dict[str, Any]:
        return self.sections.crest

    @property
    def xtb_manifest(self) -> dict[str, Any]:
        return self.sections.xtb

    @property
    def endpoint_pairing(self) -> dict[str, Any]:
        return self.sections.endpoint_pairing

    @property
    def orca_manifest(self) -> dict[str, Any]:
        return self.sections.orca


@dataclass(frozen=True)
class _RunDirWorkflowOptionDefaults:
    orca_route_line: str
    max_orca_stages: int
    max_crest_candidates: int
    max_xtb_stages: int


def _resolve_text_option_with_section(
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    section: dict[str, Any],
    section_key: str,
    default: str,
) -> str:
    explicit_text = normalize_text(explicit)
    if explicit_text:
        return explicit_text
    manifest_text = normalize_text(manifest.get(key))
    if manifest_text:
        return manifest_text
    section_text = normalize_text(section.get(section_key))
    if section_text:
        return section_text
    return default


def _int_requirement(minimum: int | None) -> str:
    if minimum is None:
        return "an integer"
    return f"an integer >= {minimum}"


def _coerce_int_option(value: Any, *, key: str, minimum: int | None = None) -> int:
    try:
        parsed = strict_int(value, field=key, minimum=minimum)
    except ValueError as exc:
        raise ValueError(f"{key} must be {_int_requirement(minimum)}. got={value!r}") from exc
    return parsed


def _resolve_int_option(
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    if explicit is not None:
        return _coerce_int_option(explicit, key=key, minimum=minimum)
    manifest_value = manifest.get(key)
    if manifest_value is None or normalize_text(manifest_value) == "":
        return default
    return _coerce_int_option(manifest_value, key=key, minimum=minimum)


def _resolve_positive_int_option(
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    default: int,
) -> int:
    return _resolve_int_option(explicit, manifest, key, default, minimum=1)


def _resolve_int_option_with_section(
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    section: dict[str, Any],
    section_key: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    if explicit is not None:
        return _coerce_int_option(explicit, key=key, minimum=minimum)
    manifest_value = manifest.get(key)
    if manifest_value is not None and normalize_text(manifest_value) != "":
        return _coerce_int_option(manifest_value, key=key, minimum=minimum)
    section_value = section.get(section_key)
    if section_value is None or normalize_text(section_value) == "":
        return default
    return _coerce_int_option(section_value, key=key, minimum=minimum)


def _resolve_positive_int_option_with_section(
    explicit: Any,
    manifest: dict[str, Any],
    key: str,
    section: dict[str, Any],
    section_key: str,
    default: int,
) -> int:
    return _resolve_int_option_with_section(
        explicit,
        manifest,
        key,
        section,
        section_key,
        default,
        minimum=1,
    )


def _manifest_workflow_root(manifest: dict[str, Any]) -> str | None:
    workflow_root = normalize_text(manifest.get("workflow_root"))
    if workflow_root:
        return workflow_root

    workflow_section = manifest.get("workflow")
    if isinstance(workflow_section, dict):
        workflow_root = normalize_text(workflow_section.get("root"))
        if workflow_root:
            return workflow_root
    return None


def _resolve_required_workflow_root(args: Any, manifest: dict[str, Any]) -> str:
    # Attribute access keeps the cli_common monkeypatch seam used by tests.
    resolved_workflow_root = cli_common._discover_workflow_root(
        getattr(args, "workflow_root", None)
    )
    if not resolved_workflow_root:
        resolved_workflow_root = cli_common._discover_workflow_root(
            _manifest_workflow_root(manifest)
        )
    if not resolved_workflow_root:
        config_path = getattr(args, "orca_auto_config", None) or getattr(args, "config", None)
        resolved_workflow_root = _cli_workflow_root_for_args(args, config_path=config_path)
    if not resolved_workflow_root:
        raise ValueError(
            "workflow_root is not configured. Set workflow_root in flow.yaml "
            "or runs_root in orca_auto.yaml."
        )
    return resolved_workflow_root


def _resolve_run_dir_core_options(
    args: Any,
    manifest: dict[str, Any],
    sections: RunDirManifestSections,
    *,
    workflow_root: str | None,
) -> dict[str, Any]:
    return {
        "workflow_root": workflow_root or _resolve_required_workflow_root(args, manifest),
        "crest_mode": _resolve_text_option_with_section(
            getattr(args, "crest_mode", None),
            manifest,
            "crest_mode",
            sections.crest,
            "mode",
            "standard",
        ),
        "priority": _resolve_int_option(getattr(args, "priority", None), manifest, "priority", 10),
    }


def _resolve_run_dir_resource_options(
    args: Any,
    manifest: dict[str, Any],
    sections: RunDirManifestSections,
) -> dict[str, Any]:
    return {
        "max_cores": _resolve_positive_int_option_with_section(
            getattr(args, "max_cores", None),
            manifest,
            "max_cores",
            sections.resources,
            "max_cores",
            8,
        ),
        "max_memory_gb": _resolve_positive_int_option_with_section(
            getattr(args, "max_memory_gb", None),
            manifest,
            "max_memory_gb",
            sections.resources,
            "max_memory_gb",
            32,
        ),
    }


def _resolve_run_dir_orca_options(
    args: Any,
    manifest: dict[str, Any],
    sections: RunDirManifestSections,
    *,
    defaults: _RunDirWorkflowOptionDefaults,
) -> dict[str, Any]:
    return {
        "max_orca_stages": _resolve_positive_int_option(
            getattr(args, "max_orca_stages", None),
            manifest,
            "max_orca_stages",
            defaults.max_orca_stages,
        ),
        "orca_route_line": _resolve_text_option_with_section(
            getattr(args, "orca_route_line", None),
            manifest,
            "orca_route_line",
            sections.orca,
            "route_line",
            defaults.orca_route_line,
        ),
        "charge": _resolve_int_option_with_section(
            getattr(args, "charge", None),
            manifest,
            "charge",
            sections.orca,
            "charge",
            0,
        ),
        "multiplicity": _resolve_int_option_with_section(
            getattr(args, "multiplicity", None),
            manifest,
            "multiplicity",
            sections.orca,
            "multiplicity",
            1,
            minimum=1,
        ),
        "boltzmann_temperature_k": optional_positive_float(
            manifest,
            "boltzmann_temperature_k",
        ),
    }


def _resolve_run_dir_stage_options(
    args: Any,
    manifest: dict[str, Any],
    *,
    defaults: _RunDirWorkflowOptionDefaults,
) -> dict[str, Any]:
    max_crest_candidates = _resolve_positive_int_option(
        getattr(args, "max_crest_candidates", None),
        manifest,
        "max_crest_candidates",
        defaults.max_crest_candidates,
    )
    return {
        "max_crest_candidates": require_crest_candidate_count(max_crest_candidates),
        "max_xtb_stages": _resolve_positive_int_option(
            getattr(args, "max_xtb_stages", None),
            manifest,
            "max_xtb_stages",
            defaults.max_xtb_stages,
        ),
    }


def _resolve_run_dir_interaction_options(
    manifest: dict[str, Any],
    *,
    complex_charge: int | None = None,
    complex_multiplicity: int | None = None,
) -> dict[str, Any]:
    # Validate at admission for EVERY template so a malformed (or misplaced)
    # block fails closed with a clear error instead of being silently dropped;
    # only conformer_screening actually consumes the normalized result. The
    # workflow factory re-normalizes idempotently as the canonical gate.
    interaction_energy = normalize_interaction_energy_block(manifest.get("interaction_energy"))
    if complex_charge is not None and complex_multiplicity is not None:
        validate_interaction_energy_state_balance(
            interaction_energy,
            complex_charge=complex_charge,
            complex_multiplicity=complex_multiplicity,
        )
    return {
        "interaction_energy": interaction_energy,
        "rmsd_dedup": normalize_rmsd_dedup_block(manifest.get("rmsd_dedup")),
    }


def _resolve_run_dir_workflow_options(
    args: Any,
    manifest: dict[str, Any],
    sections: RunDirManifestSections,
    *,
    default_orca_route_line: str,
    default_max_orca_stages: int,
    default_max_crest_candidates: int = 3,
    default_max_xtb_stages: int = 3,
    workflow_root: str | None = None,
    workflow_type: str = "",
) -> RunDirWorkflowOptions:
    defaults = _RunDirWorkflowOptionDefaults(
        orca_route_line=default_orca_route_line,
        max_orca_stages=default_max_orca_stages,
        max_crest_candidates=default_max_crest_candidates,
        max_xtb_stages=default_max_xtb_stages,
    )

    orca_options = _resolve_run_dir_orca_options(args, manifest, sections, defaults=defaults)
    interaction_options = _resolve_run_dir_interaction_options(
        manifest,
        complex_charge=orca_options["charge"],
        complex_multiplicity=orca_options["multiplicity"],
    )
    if workflow_type:
        validate_conformer_postprocessing_template(
            workflow_type,
            interaction_energy=interaction_options["interaction_energy"],
            rmsd_dedup=interaction_options["rmsd_dedup"],
        )
    return RunDirWorkflowOptions(
        **_resolve_run_dir_core_options(args, manifest, sections, workflow_root=workflow_root),
        **_resolve_run_dir_resource_options(args, manifest, sections),
        **orca_options,
        **_resolve_run_dir_stage_options(args, manifest, defaults=defaults),
        **interaction_options,
    )


def _resolve_run_dir_workflow_option_bundle(
    args: Any,
    manifest: dict[str, Any],
    sections: RunDirManifestSections,
    *,
    default_orca_route_line: str,
    default_max_orca_stages: int,
    default_max_crest_candidates: int = 3,
    default_max_xtb_stages: int = 3,
    workflow_root: str | None = None,
    workflow_type: str = "",
) -> tuple[RunDirWorkflowOptions, dict[str, Any]]:
    options = _resolve_run_dir_workflow_options(
        args,
        manifest,
        sections,
        default_orca_route_line=default_orca_route_line,
        default_max_orca_stages=default_max_orca_stages,
        default_max_crest_candidates=default_max_crest_candidates,
        default_max_xtb_stages=default_max_xtb_stages,
        workflow_root=workflow_root,
        workflow_type=workflow_type,
    )
    return options, options.common_kwargs()


__all__ = [
    "RUN_DIR_COMMON_WORKFLOW_OPTION_FIELDS",
    "RunDirManifestSections",
    "RunDirWorkflowConfig",
    "RunDirWorkflowOptions",
    "_resolve_positive_int_option",
    "_resolve_positive_int_option_with_section",
]
