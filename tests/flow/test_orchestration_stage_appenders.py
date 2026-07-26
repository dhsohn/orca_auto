from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow import endpoint_pairing as endpoint_pairing_module
from orca_auto.flow._orca_stage_materialization import render_orca_input
from orca_auto.flow.contracts import CrestDownstreamPolicy, WorkflowStageInput
from orca_auto.flow.endpoint_pairing import (
    MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS,
    EndpointPairingPolicy,
    select_endpoint_pairs,
)
from orca_auto.flow.orchestration import (
    crest_orca_materialization,
    reaction_materialization,
    reaction_orca_materialization,
)
from orca_auto.flow.orchestration.materialization import (
    append_crest_orca_stages_impl,
    append_reaction_orca_stages_impl,
    append_reaction_xtb_stages_impl,
)
from tests.flow.orchestration_services import orchestration_services


def _candidate(
    path: str,
    *,
    source_job_id: str,
    source_job_type: str,
    reaction_key: str,
    rank: int,
    kind: str,
    selected_input_xyz: str | None = None,
    selected: bool = True,
    score: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> WorkflowStageInput:
    return WorkflowStageInput(
        source_job_id=source_job_id,
        source_job_type=source_job_type,
        reaction_key=reaction_key,
        selected_input_xyz=selected_input_xyz or path,
        rank=rank,
        kind=kind,
        artifact_path=path,
        selected=selected,
        score=score,
        metadata=metadata or {},
    )


def _orca_stage_result(**kwargs: Any) -> SimpleNamespace:
    candidate = kwargs["candidate"]
    stage = {
        "stage_id": kwargs["stage_id"],
        "status": "planned",
        "metadata": {},
        "input_artifacts": [
            {
                "kind": kwargs["input_artifact_kind"],
                "path": candidate.artifact_path,
                "selected": candidate.selected,
            }
        ],
        "task": {
            "engine": "orca",
            "task_kind": kwargs["task_kind"],
            "status": "planned",
            "payload": {"reaction_dir": ""},
            "metadata": {"source_candidate_path": candidate.artifact_path},
        },
    }
    return SimpleNamespace(to_dict=lambda: stage)


def _write_xyz(path: Path, coords: list[tuple[str, float, float, float]]) -> str:
    path.write_text(
        "\n".join(
            [
                str(len(coords)),
                path.stem,
                *(f"{element} {x:.6f} {y:.6f} {z:.6f}" for element, x, y, z in coords),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def test_generic_crest_handoff_policy_is_not_reaction_candidate_cap() -> None:
    assert CrestDownstreamPolicy.build(max_candidates=33).max_candidates == 33


def test_endpoint_pairing_rejects_unbounded_distance_fingerprint_atoms(tmp_path: Path) -> None:
    atom_count = MAX_ENDPOINT_PAIRING_COMPARISON_ATOMS + 1
    coords = [("H", float(index), 0.0, 0.0) for index in range(atom_count)]
    reactant_path = _write_xyz(tmp_path / "reactant-large.xyz", coords)
    product_path = _write_xyz(tmp_path / "product-large.xyz", coords)

    reactant = _candidate(
        reactant_path,
        source_job_id="crest_r",
        source_job_type="crest",
        reaction_key="reactant",
        rank=1,
        kind="conformer",
    )
    product = _candidate(
        product_path,
        source_job_id="crest_p",
        source_job_type="crest",
        reaction_key="product",
        rank=1,
        kind="conformer",
    )

    with pytest.raises(ValueError, match="comparison atom count exceeds"):
        select_endpoint_pairs(
            [reactant],
            [product],
            policy=EndpointPairingPolicy.from_raw({"max_distance_rmsd": 0.5}),
        )


def test_endpoint_pairing_loads_each_ensemble_once_per_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reactant_path = _write_xyz(
        tmp_path / "reactant.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0)],
    )
    product_path = _write_xyz(
        tmp_path / "product.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0)],
    )
    original_loader = endpoint_pairing_module.load_output_xyz_frames
    loaded_paths: list[Path] = []

    def counted_loader(path: Path) -> tuple[Any, ...]:
        loaded_paths.append(path)
        return original_loader(path)

    monkeypatch.setattr(endpoint_pairing_module, "load_output_xyz_frames", counted_loader)
    reactants = [
        _candidate(
            reactant_path,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key=f"reactant_{index}",
            rank=index,
            kind="conformer",
        )
        for index in (1, 2)
    ]
    products = [
        _candidate(
            product_path,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key=f"product_{index}",
            rank=index,
            kind="conformer",
        )
        for index in (1, 2)
    ]

    pairs = select_endpoint_pairs(
        reactants,
        products,
        policy=EndpointPairingPolicy.from_raw(
            {"comparison_atoms": [1, 2], "max_pairs": 2},
        ),
    )

    assert len(pairs) == 2
    assert loaded_paths == [Path(reactant_path), Path(product_path)]


def test_endpoint_pairing_handles_extreme_finite_coordinates(tmp_path: Path) -> None:
    reactant_path = _write_xyz(
        tmp_path / "reactant-extreme.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 1.0e200, 0.0, 0.0)],
    )
    product_path = _write_xyz(
        tmp_path / "product-extreme.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 0.9e200, 0.0, 0.0)],
    )

    pairs = select_endpoint_pairs(
        [
            _candidate(
                reactant_path,
                source_job_id="crest_r",
                source_job_type="crest",
                reaction_key="reactant",
                rank=1,
                kind="conformer",
            )
        ],
        [
            _candidate(
                product_path,
                source_job_id="crest_p",
                source_job_type="crest",
                reaction_key="product",
                rank=1,
                kind="conformer",
            )
        ],
        policy=EndpointPairingPolicy.from_raw(
            {"comparison_atoms": [1, 2], "max_distance_rmsd": 2.0e199},
        ),
    )

    assert len(pairs) == 1
    assert pairs[0].metadata["distance_fingerprint_rmsd"] == pytest.approx(1.0e199)


@pytest.mark.parametrize(
    "policy",
    [
        {"comparison_atoms": [999, 1000]},
        {"moving_atoms": [999], "max_distance_rmsd": 0.5},
    ],
)
def test_endpoint_pairing_revalidates_atom_indices_against_durable_geometry(
    tmp_path: Path,
    policy: dict[str, object],
) -> None:
    reactant_path = _write_xyz(
        tmp_path / "reactant-small.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0)],
    )
    product_path = _write_xyz(
        tmp_path / "product-small.xyz",
        [("H", 0.0, 0.0, 0.0), ("H", 1.0, 0.0, 0.0)],
    )

    with pytest.raises(ValueError, match="atom indices must be within"):
        select_endpoint_pairs(
            [
                _candidate(
                    reactant_path,
                    source_job_id="crest_r",
                    source_job_type="crest",
                    reaction_key="reactant",
                    rank=1,
                    kind="conformer",
                )
            ],
            [
                _candidate(
                    product_path,
                    source_job_id="crest_p",
                    source_job_type="crest",
                    reaction_key="product",
                    rank=1,
                    kind="conformer",
                )
            ],
            policy=EndpointPairingPolicy.from_raw(policy),
        )


@pytest.mark.parametrize(
    ("charge", "multiplicity"),
    [(1.9, 2), (1, 2.9), (True, 1), (0, True)],
)
def test_orca_input_renderer_rejects_lossy_electronic_state(
    charge: object,
    multiplicity: object,
) -> None:
    with pytest.raises(ValueError, match=r"(?:charge|multiplicity) must be an integer"):
        render_orca_input(
            route_line="! r2scan-3c",
            charge=charge,  # type: ignore[arg-type]
            multiplicity=multiplicity,  # type: ignore[arg-type]
            max_cores=1,
            max_memory_gb=1,
            xyz_filename="input.xyz",
        )


def test_disabled_endpoint_pairing_caps_eight_by_eight_cartesian_product() -> None:
    reactants = [
        _candidate(
            f"/tmp/reactant_{index}.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key=f"reactant_{index}",
            rank=index,
            kind="conformer",
        )
        for index in range(1, 9)
    ]
    products = [
        _candidate(
            f"/tmp/product_{index}.xyz",
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key=f"product_{index}",
            rank=index,
            kind="conformer",
        )
        for index in range(1, 9)
    ]

    policy = EndpointPairingPolicy.from_raw(None, default_max_pairs=8)
    pairs = select_endpoint_pairs(reactants, products, policy=policy)

    assert policy.enabled is False
    assert policy.max_pairs == 8
    assert len(pairs) == 8
    assert [(pair.reactant.rank, pair.product.rank) for pair in pairs] == [
        (index, index) for index in range(1, 9)
    ]


def test_endpoint_pairing_rejects_inputs_above_candidate_ceiling() -> None:
    reactants = [
        _candidate(
            f"/tmp/reactant_{index}.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key=f"reactant_{index}",
            rank=index,
            kind="conformer",
        )
        for index in range(1, 34)
    ]
    product = _candidate(
        "/tmp/product.xyz",
        source_job_id="crest_p",
        source_job_type="crest",
        reaction_key="product",
        rank=1,
        kind="conformer",
    )

    with pytest.raises(ValueError, match="per-side candidate limit"):
        select_endpoint_pairs(
            reactants,
            [product],
            policy=EndpointPairingPolicy.from_raw(None, default_max_pairs=3),
        )


@pytest.mark.parametrize("field", ["max_distance_rmsd", "max_rmsd", "rank_weight"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "nan", "-inf"])
def test_endpoint_pairing_rejects_nonfinite_policy_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="must be a finite number"):
        EndpointPairingPolicy.from_raw({"enabled": True, field: value})


def test_endpoint_pairing_never_falls_back_across_element_order_mismatch(tmp_path: Path) -> None:
    reactant = _write_xyz(
        tmp_path / "reactant.xyz",
        [("H", 0, 0, 0), ("O", 0, 0, 1)],
    )
    product = _write_xyz(
        tmp_path / "product.xyz",
        [("O", 0, 0, 0), ("H", 0, 0, 1)],
    )
    policy = EndpointPairingPolicy.from_raw(
        {
            "enabled": True,
            "comparison_atoms": [1, 2],
            "fallback_to_ranked": True,
        }
    )

    pairs = select_endpoint_pairs(
        [
            _candidate(
                reactant,
                source_job_id="r",
                source_job_type="crest",
                reaction_key="rxn",
                rank=1,
                kind="conformer",
            )
        ],
        [
            _candidate(
                product,
                source_job_id="p",
                source_job_type="crest",
                reaction_key="rxn",
                rank=1,
                kind="conformer",
            )
        ],
        policy=policy,
    )

    assert pairs == ()


@pytest.mark.parametrize(
    "raw",
    [
        {"comparison_atoms": [1.5, 2]},
        {"comparison_atoms": [True, 2]},
        {"comparison_atoms": ["bogus"]},
        {"max_distance_rmsd": -0.1},
        {"rank_weight": -1},
        {"comparison_atoms": [1, 2], "atoms": [1, 2]},
        {"moving_atoms": [1], "excluded_atoms": [1]},
        {"comparison_atoms": [1, 2], "moving_atoms": [2]},
        {"max_distance_rmsd": 0.1, "max_rmsd": 0.2},
        {"enabled": True, "mode": "enabled"},
    ],
)
def test_endpoint_pairing_rejects_lossy_scientific_policy_values(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="endpoint_pairing"):
        EndpointPairingPolicy.from_raw(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "tru"},
        {"enabled": 1},
        {"fallback_to_ranked": "maybe"},
        {"fallback_to_ranked": 0},
        {"mode": "disabledd"},
        {"mode": True},
        {"enable": False},
        {"fallback_to_rankd": False},
        "geometry",
        1,
        ["enabled"],
    ],
)
def test_endpoint_pairing_rejects_invalid_boolean_and_shorthand_types(raw: object) -> None:
    with pytest.raises(ValueError, match="endpoint_pairing"):
        EndpointPairingPolicy.from_raw(raw)


@pytest.mark.parametrize("raw", [True, "true", "yes", "on", "enabled", "1"])
def test_endpoint_pairing_accepts_supported_enabled_shorthand(raw: object) -> None:
    assert EndpointPairingPolicy.from_raw(raw).enabled is True


@pytest.mark.parametrize("raw", [False, "false", "no", "off", "disabled", "0"])
def test_endpoint_pairing_accepts_supported_disabled_shorthand(raw: object) -> None:
    assert EndpointPairingPolicy.from_raw(raw).enabled is False


@pytest.mark.parametrize("mode", ["true", "yes", "on", "enabled", "1"])
def test_endpoint_pairing_accepts_supported_enabled_mapping_modes(mode: str) -> None:
    assert EndpointPairingPolicy.from_raw({"mode": mode}).enabled is True


@pytest.mark.parametrize("mode", ["false", "no", "off", "disabled", "0", "none"])
def test_endpoint_pairing_accepts_supported_disabled_mapping_modes(mode: str) -> None:
    assert EndpointPairingPolicy.from_raw({"mode": mode}).enabled is False


@pytest.mark.parametrize("max_handoff_retries", [0, 4])
def test_append_reaction_xtb_stages_caps_cartesian_product(
    tmp_path: Path,
    max_handoff_retries: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_01",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 3,
                    "max_xtb_handoff_retries": max_handoff_retries,
                    "endpoint_pairing": {"enabled": False, "max_pairs": 0},
                    "charge": -1,
                    "multiplicity": 2,
                    "xtb_job_manifest": {"gfn": 1},
                }
            }
        },
    }
    reactant_inputs = [
        _candidate(
            "/tmp/reactant_a.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            "/tmp/reactant_b.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            "/tmp/product_a.xyz",
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            "/tmp/product_b.xyz",
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]
    observed_pair_limits: list[int] = []

    def select_pairs(reactants: Any, products: Any, *, policy: Any) -> tuple[Any, ...]:
        observed_pair_limits.append(policy.max_pairs)
        return endpoint_pairing_module.select_endpoint_pairs(
            reactants,
            products,
            policy=policy,
        )

    monkeypatch.setattr(
        reaction_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: (
            "reactant_contract"
            if stage["metadata"]["input_role"] == "reactant"
            else "product_contract"
        ),
    )
    deps = orchestration_services(
        overrides={
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
            "select_endpoint_pairs": select_pairs,
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        services=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert [stage["stage_id"] for stage in xtb_stages] == [
        "xtb_path_search_01",
        "xtb_path_search_02",
        "xtb_path_search_03",
    ]
    assert all(
        stage["task"]["payload"]["max_handoff_retries"] == max_handoff_retries
        for stage in xtb_stages
    )
    assert observed_pair_limits == [3]
    assert all(
        stage["task"]["payload"]["job_manifest_overrides"]
        == {
            "charge": -1,
            "uhf": 1,
            "gfn": 1,
        }
        for stage in xtb_stages
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_crest_candidates", 2.5),
        ("max_crest_candidates", 33),
        ("max_xtb_stages", 2.5),
    ],
)
def test_append_reaction_xtb_stages_revalidates_durable_candidate_caps(
    tmp_path: Path,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_invalid_cap",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 2,
                    field: value,
                }
            }
        },
    }
    candidate = _candidate(
        "/tmp/candidate.xyz",
        source_job_id="crest",
        source_job_type="crest",
        reaction_key="candidate",
        rank=1,
        kind="conformer",
    )
    monkeypatch.setattr(
        reaction_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: stage["metadata"]["input_role"],
    )
    deps = orchestration_services(
        overrides={
            "select_crest_downstream_inputs": lambda contract, policy: (candidate,),
        }
    )

    with pytest.raises(ValueError, match=field):
        append_reaction_xtb_stages_impl(
            payload,
            workspace_dir=tmp_path,
            crest_config="/tmp/crest.yaml",
            services=deps,
        )


def test_append_reaction_xtb_stages_fails_when_completed_crest_has_no_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_empty_crest",
        "template_name": "reaction_ts_search",
        "status": "running",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 1,
                    "max_xtb_handoff_retries": 2,
                }
            }
        },
    }
    monkeypatch.setattr(
        reaction_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: stage["metadata"]["input_role"],
    )
    deps = orchestration_services(
        overrides={
            "select_crest_downstream_inputs": lambda contract, policy: (
                ()
                if contract == "reactant"
                else (
                    _candidate(
                        "/tmp/product.xyz",
                        source_job_id="crest_p",
                        source_job_type="crest",
                        reaction_key="p",
                        rank=1,
                        kind="conformer",
                    ),
                )
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload, workspace_dir=tmp_path, crest_config="/tmp/crest.yaml", services=deps
    )

    assert created is False
    assert payload["metadata"]["workflow_error"]["scope"] == ("reaction_ts_search_crest_handoff")


def test_append_reaction_xtb_stages_filters_endpoint_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_pairing",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 1,
                    "max_xtb_handoff_retries": 2,
                    "endpoint_pairing": {
                        "enabled": True,
                        "comparison_atoms": [1, 2, 3],
                        "max_distance_rmsd": 0.05,
                    },
                }
            }
        },
    }
    r_a = _write_xyz(
        tmp_path / "reactant_a.xyz",
        [("H", 0, 0, 0), ("H", 1, 0, 0), ("H", 0, 1, 0)],
    )
    r_b = _write_xyz(
        tmp_path / "reactant_b.xyz",
        [("H", 0, 0, 0), ("H", 2, 0, 0), ("H", 0, 2, 0)],
    )
    p_a = _write_xyz(
        tmp_path / "product_a.xyz",
        [("H", 5, 5, 0), ("H", 6, 5, 0), ("H", 5, 6, 0)],
    )
    p_b = _write_xyz(
        tmp_path / "product_b.xyz",
        [("H", 0, 0, 0), ("H", 2, 0, 0), ("H", 0, 2, 0)],
    )
    reactant_inputs = [
        _candidate(
            r_a,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            r_b,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            p_a,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            p_b,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]

    monkeypatch.setattr(
        reaction_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: (
            "reactant_contract"
            if stage["metadata"]["input_role"] == "reactant"
            else "product_contract"
        ),
    )
    deps = orchestration_services(
        overrides={
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        services=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert len(xtb_stages) == 1
    assert xtb_stages[0]["task"]["payload"]["reactant_source"]["artifact_path"] == r_a
    assert xtb_stages[0]["task"]["payload"]["product_source"]["artifact_path"] == p_a
    pairing = xtb_stages[0]["metadata"]["endpoint_pairing"]
    assert pairing["strategy"] == "distance_fingerprint"
    assert pairing["distance_fingerprint_rmsd"] == 0.0
    assert payload["metadata"]["endpoint_pairing"]["candidate_pair_count"] == 4
    assert payload["metadata"]["endpoint_pairing"]["selected_pair_count"] == 1


def test_append_reaction_xtb_stages_can_exclude_moving_atoms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_pairing_exclude",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 1,
                    "max_xtb_handoff_retries": 2,
                    "endpoint_pairing": {
                        "enabled": True,
                        "moving_atoms": [4],
                        "max_distance_rmsd": 0.05,
                    },
                }
            }
        },
    }
    r_a = _write_xyz(
        tmp_path / "reactant_a.xyz",
        [("C", 0, 0, 0), ("C", 1, 0, 0), ("C", 0, 1, 0), ("H", 0, 0, 2)],
    )
    p_a = _write_xyz(
        tmp_path / "product_a.xyz",
        [("C", 5, 5, 0), ("C", 6, 5, 0), ("C", 5, 6, 0), ("H", 12, 12, 12)],
    )
    r_b = _write_xyz(
        tmp_path / "reactant_b.xyz",
        [("C", 0, 0, 0), ("C", 2, 0, 0), ("C", 0, 2, 0), ("H", 0, 0, 2)],
    )
    p_b = _write_xyz(
        tmp_path / "product_b.xyz",
        [("C", 0, 0, 0), ("C", 3, 0, 0), ("C", 0, 3, 0), ("H", 0, 0, 2)],
    )
    reactant_inputs = [
        _candidate(
            r_a,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            r_b,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            p_a,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            p_b,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]

    monkeypatch.setattr(
        reaction_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: (
            "reactant_contract"
            if stage["metadata"]["input_role"] == "reactant"
            else "product_contract"
        ),
    )
    deps = orchestration_services(
        overrides={
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        services=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert len(xtb_stages) == 1
    assert xtb_stages[0]["task"]["payload"]["reactant_source"]["artifact_path"] == r_a
    assert xtb_stages[0]["task"]["payload"]["product_source"]["artifact_path"] == p_a
    pairing = xtb_stages[0]["metadata"]["endpoint_pairing"]
    assert pairing["comparison_atoms"] == [1, 2, 3]
    assert pairing["excluded_atoms"] == [4]
    assert pairing["distance_fingerprint_rmsd"] == 0.0


def test_append_reaction_xtb_stages_waits_for_latest_product_crest_stage(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_wait",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "crest_product_old",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "crest_product_new",
                "status": "running",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "running"},
            },
        ],
        "metadata": {"request": {"parameters": {"max_crest_candidates": 2}}},
    }

    deps = orchestration_services()

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        services=deps,
    )

    assert created is False
    assert all(stage.get("task", {}).get("engine") != "xtb" for stage in payload["stages"])


def test_append_reaction_orca_stages_sets_xtb_handoff_workflow_error_when_no_candidate_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_02",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_01"},
                },
            }
        ],
    }
    contract = SimpleNamespace(
        job_id="xtb_job_01",
        job_type="path_search",
        candidate_details=(),
        selected_candidate_paths=(),
    )

    monkeypatch.setattr(
        reaction_orca_materialization,
        "reaction_ts_guess_error_impl",
        lambda current_contract, **kwargs: {
            "reason": "xtb_ts_guess_missing",
            "message": "missing ts guess",
        },
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (),
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    xtb_stage = payload["stages"][0]
    assert created is False
    assert xtb_stage["metadata"]["reaction_handoff_status"] == "failed"
    assert xtb_stage["metadata"]["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "reaction_ts_search_xtb_handoff",
        "stage_id": "xtb_path_search_01",
        "job_id": "xtb_job_01",
        "reason": "xtb_ts_guess_missing",
        "message": "missing ts guess",
    }


def test_append_reaction_orca_stages_fails_workflow_when_all_xtb_stages_failed(
    tmp_path: Path,
) -> None:
    # Every xTB path search stage ended terminal-failed (crash or submission
    # failure), so no ORCA candidate stage is ever materialized. The reaction TS
    # search produced no TS guess and must be recorded as a workflow FAILURE, not
    # left without a workflow_error (which recompute_workflow_status reports as
    # COMPLETED once every stage is terminal).
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_all_xtb_failed",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "failed",
                "metadata": {},
                "task": {"engine": "xtb", "payload": {"job_dir": "/tmp/xtb_job_01"}},
            },
            {
                "stage_id": "xtb_path_search_02",
                "status": "submission_failed",
                "metadata": {},
                "task": {"engine": "xtb", "payload": {"job_dir": "/tmp/xtb_job_02"}},
            },
        ],
    }

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=orchestration_services(),
    )

    assert created is False
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "reaction_ts_search_xtb_handoff",
        "reason": "reaction_ts_search_xtb_phase_failed",
        "message": "All xTB path search stages failed; no TS guess was produced.",
        "stage_id": "xtb_path_search_01",
    }


def test_append_reaction_orca_stages_waits_when_xtb_contract_is_missing(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_missing_xtb_contract",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_missing"},
                },
            }
        ],
    }

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                FileNotFoundError("xTB artifact files not found")
            ),
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert created is False
    assert "workflow_error" not in payload["metadata"]
    assert "reaction_handoff_status" not in payload["stages"][0]["metadata"]


def test_append_reaction_orca_stages_propagates_corrupt_xtb_contract(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_corrupt_xtb_contract",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_corrupt"},
                },
            }
        ],
    }

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                ValueError("corrupt xTB artifact payload")
            ),
        }
    )

    with pytest.raises(ValueError, match="corrupt xTB artifact payload"):
        append_reaction_orca_stages_impl(
            payload,
            workspace_dir=tmp_path,
            xtb_config="/tmp/xtb.yaml",
            orca_config="/tmp/orca.yaml",
            services=deps,
        )


def test_append_reaction_orca_stages_waits_for_all_xtb_children_to_finish(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_wait_orca",
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "status": "completed",
                    "payload": {"job_dir": "/tmp/xtb_done"},
                },
            },
            {
                "stage_id": "xtb_path_search_02",
                "status": "queued",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "status": "submitted",
                    "payload": {"job_dir": "/tmp/xtb_queued"},
                },
            },
        ],
    }

    deps = orchestration_services(
        overrides={
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("ORCA batching should wait for terminal xTB phases")
            )
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert created is False
    assert [stage["stage_id"] for stage in payload["stages"]] == [
        "xtb_path_search_01",
        "xtb_path_search_02",
    ]


def test_append_reaction_orca_stages_appends_unattempted_candidate_without_mutating_failed_stage(
    tmp_path: Path,
) -> None:
    first_candidate = _candidate(
        "/tmp/candidate_01.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=1,
        kind="ts_guess",
        score=-10.0,
    )
    second_candidate = _candidate(
        "/tmp/candidate_02.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=2,
        kind="ts_guess",
        score=-9.0,
    )
    third_candidate = _candidate(
        "/tmp/candidate_03.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=3,
        kind="ts_guess",
        score=-8.0,
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_03",
        "metadata": {
            "workflow_error": {"scope": "reaction_ts_search_orca_candidate_exhausted"},
            "request": {
                "parameters": {
                    "max_orca_stages": 2,
                    "orca_route_line": "! custom route",
                }
            },
        },
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_02"},
                },
            },
            {
                "stage_id": "orca_optts_freq_01",
                "status": "failed",
                "metadata": {"analyzer_status": "ts_not_found"},
                "task": {
                    "engine": "orca",
                    "metadata": {"source_candidate_path": first_candidate.artifact_path},
                },
            },
        ],
    }
    contract = SimpleNamespace(
        job_id="xtb_job_02",
        job_type="path_search",
        candidate_details=(),
        selected_candidate_paths=(),
    )

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (
                first_candidate,
                second_candidate,
                third_candidate,
            ),
            "build_materialized_orca_stage": _orca_stage_result,
            "now_utc_iso": lambda: "2026-04-19T15:00:00+00:00",
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    latest_existing = payload["stages"][1]
    appended = payload["stages"][2]
    assert created is True
    assert latest_existing["metadata"] == {"analyzer_status": "ts_not_found"}
    assert "workflow_error" not in payload["metadata"]
    assert appended["stage_id"] == "orca_optts_freq_02"
    assert appended["metadata"]["reaction_candidate_attempt_index"] == 2
    assert appended["metadata"]["reaction_candidate_pool_size"] == 3
    assert appended["metadata"]["reaction_candidate_limit"] == 2
    assert appended["metadata"]["reaction_candidates_omitted_by_limit"] == 1
    assert appended["metadata"]["reaction_remaining_candidates_after_this"] == 0
    assert len(payload["stages"]) == 3


def test_append_reaction_orca_stages_fails_workflow_when_all_orca_candidates_fail(
    tmp_path: Path,
) -> None:
    # The xTB handoff succeeded and ORCA OptTS candidate stages were materialized,
    # but every candidate failed to verify a TS and none remain to try. The reaction
    # TS search produced no transition state and must be recorded as FAILED (mirrors
    # the scan_ts_search candidate-exhaustion guard); a failed reaction ORCA stage is
    # engine-role non-fatal, so recompute_workflow_status would otherwise report
    # COMPLETED.
    first_candidate = _candidate(
        "/tmp/candidate_01.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=1,
        kind="ts_guess",
        score=-10.0,
    )
    second_candidate = _candidate(
        "/tmp/candidate_02.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=2,
        kind="ts_guess",
        score=-9.0,
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_all_orca_failed",
        "metadata": {"request": {"parameters": {"max_orca_stages": 3}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {"engine": "xtb", "payload": {"job_dir": "/tmp/xtb_job_02"}},
            },
            {
                "stage_id": "orca_optts_freq_01",
                "status": "failed",
                "metadata": {"analyzer_status": "ts_not_found"},
                "task": {
                    "engine": "orca",
                    "metadata": {"source_candidate_path": first_candidate.artifact_path},
                },
            },
            {
                "stage_id": "orca_optts_freq_02",
                "status": "failed",
                "metadata": {"analyzer_status": "geom_not_converged"},
                "task": {
                    "engine": "orca",
                    "metadata": {"source_candidate_path": second_candidate.artifact_path},
                },
            },
        ],
    }
    contract = SimpleNamespace(
        job_id="xtb_job_02",
        job_type="path_search",
        candidate_details=(),
        selected_candidate_paths=(),
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (
                first_candidate,
                second_candidate,
            ),
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert created is False
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "reaction_ts_search_orca_candidate_exhausted",
        "stage_id": "orca_optts_freq_01",
        "reason": "ts_candidates_exhausted",
        "message": (
            "All reaction TS candidates reached terminal states; none of the attempted "
            "candidates verified a transition state."
        ),
    }


def test_append_reaction_orca_stages_records_submission_rejections_in_exhaustion_error(
    tmp_path: Path,
) -> None:
    """Candidates rejected at queue submission must not read as 'attempted'."""
    candidate = _candidate(
        "/tmp/candidate_01.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=1,
        kind="ts_guess",
        score=-10.0,
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_submission_rejected",
        "metadata": {"request": {"parameters": {"max_orca_stages": 1}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {"engine": "xtb", "payload": {"job_dir": "/tmp/xtb_job_02"}},
            },
            {
                "stage_id": "orca_optts_freq_01",
                "status": "submission_failed",
                "metadata": {"reason": "invalid_submission_input"},
                "task": {
                    "engine": "orca",
                    "metadata": {"source_candidate_path": candidate.artifact_path},
                },
            },
        ],
    }
    contract = SimpleNamespace(
        job_id="xtb_job_02",
        job_type="path_search",
        candidate_details=(),
        selected_candidate_paths=(),
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / str(kwargs.get("engine") or "orca")
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (candidate,),
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert created is False
    workflow_error = payload["metadata"]["workflow_error"]
    assert workflow_error["reason"] == "ts_candidates_exhausted"
    assert "rejected before execution" in workflow_error["message"]
    assert "submission_error_detail" in workflow_error["message"]


def test_append_reaction_orca_stages_materializes_under_workflow_orca_stage_root(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        "/tmp/candidate_local.xyz",
        source_job_id="xtb_job_local",
        source_job_type="path_search",
        reaction_key="rxn_local",
        rank=1,
        kind="ts_guess",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_local",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 1}},
            "workspace_dir": str((tmp_path / "wf_reaction_local").resolve()),
        },
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_local"},
                },
            }
        ],
    }
    contract = SimpleNamespace(
        job_id="xtb_job_local",
        job_type="path_search",
        candidate_details=(),
        selected_candidate_paths=(),
    )
    build_calls: list[dict[str, Any]] = []

    def fake_build_materialized_orca_stage(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _orca_stage_result(**kwargs)

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / "orca_allowed"
            },
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (candidate,),
            "build_materialized_orca_stage": fake_build_materialized_orca_stage,
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path / "wf_reaction_local",
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        services=deps,
    )

    assert created is True
    assert build_calls[0]["workspace_dir"] == (tmp_path / "wf_reaction_local" / "03_orca").resolve()
    assert build_calls[0]["stage_root_name"] == ""


def test_append_crest_orca_stages_materializes_orca_stages_from_completed_crest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crest_candidate = _candidate(
        "/tmp/crest_conformer.xyz",
        source_job_id="crest_job_01",
        source_job_type="conformer_search",
        reaction_key="rxn_crest",
        rank=1,
        kind="conformer",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_01",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 1}},
            "workspace_dir": str((tmp_path / "wf_conf_01").resolve()),
        },
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest"},
            }
        ],
    }
    build_calls: list[dict[str, Any]] = []

    def fake_build_materialized_orca_stage(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _orca_stage_result(**kwargs)

    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / "orca_allowed"
            },
            "select_crest_downstream_inputs": lambda contract, policy: (crest_candidate,),
            "build_materialized_orca_stage": fake_build_materialized_orca_stage,
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        services=deps,
    )

    assert created is True
    assert build_calls[0]["workspace_dir"] == (tmp_path / "wf_conf_01" / "03_orca").resolve()
    assert build_calls[0]["stage_root_name"] == ""
    assert payload["stages"][-1]["stage_id"] == "orca_conformer_01"
    assert payload["stages"][-1]["task"]["engine"] == "orca"


def test_append_crest_orca_stages_fails_workflow_when_all_conformers_fail(
    tmp_path: Path,
) -> None:
    # conformer_screening: the CREST stage completed and ORCA conformer opt stages
    # were materialized, but every one failed to optimize. No optimized conformer
    # was produced, so the workflow must be recorded as FAILED (a failed ORCA
    # conformer stage is engine-role non-fatal, so recompute would otherwise report
    # COMPLETED).
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_all_failed",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 2}},
            "workspace_dir": str((tmp_path / "wf_conf_all_failed").resolve()),
        },
        "stages": [
            {"stage_id": "crest_stage_01", "status": "completed", "task": {"engine": "crest"}},
            {
                "stage_id": "orca_conformer_01",
                "status": "failed",
                "metadata": {"analyzer_status": "geom_not_converged"},
                "task": {"engine": "orca"},
            },
            {
                "stage_id": "orca_conformer_02",
                "status": "failed",
                "metadata": {"analyzer_status": "scf_not_converged"},
                "task": {"engine": "orca"},
            },
        ],
    }

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        services=orchestration_services(),
    )

    assert created is False
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "conformer_screening_orca_conformers_exhausted",
        "stage_id": "orca_conformer_01",
        "reason": "conformers_failed",
        "message": "All conformer optimization stages failed; no optimized conformer was produced.",
    }


def test_append_crest_orca_stages_fails_when_completed_crest_has_no_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_empty_crest",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 2}},
            "workspace_dir": str(tmp_path),
        },
        "stages": [
            {"stage_id": "crest_stage_01", "status": "completed", "task": {"engine": "crest"}}
        ],
    }
    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {"allowed_root": tmp_path},
            "select_crest_downstream_inputs": lambda contract, policy: (),
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer.xyz",
        inp_filename="conformer.inp",
        services=deps,
    )

    assert created is False
    assert payload["metadata"]["workflow_error"]["scope"] == ("conformer_screening_crest_handoff")


def test_append_crest_orca_stages_materializes_twenty_orca_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crest_candidates = tuple(
        _candidate(
            f"/tmp/crest_conformer_{index:02d}.xyz",
            source_job_id="crest_job_20",
            source_job_type="conformer_search",
            reaction_key="mol_20",
            rank=index,
            kind="conformer",
        )
        for index in range(1, 21)
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_20",
        "metadata": {"request": {"parameters": {"max_orca_stages": 20}}},
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest"},
            }
        ],
    }

    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / "orca_allowed"
            },
            "select_crest_downstream_inputs": lambda contract, policy: crest_candidates,
            "build_materialized_orca_stage": _orca_stage_result,
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        services=deps,
    )

    orca_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "orca"
    ]
    assert created is True
    assert len(orca_stages) == 20
    assert orca_stages[0]["stage_id"] == "orca_conformer_01"
    assert orca_stages[-1]["stage_id"] == "orca_conformer_20"


def test_reaction_materialization_requires_persisted_stage_budgets() -> None:
    """Creation always persists the stage budgets; a payload missing one is
    corrupt or hand-edited and must fail closed instead of receiving a
    guessed default."""

    from orca_auto.flow.orchestration.support import required_stage_budget as _required_param

    assert _required_param({"max_xtb_stages": 9}, "max_xtb_stages") == 9
    with pytest.raises(ValueError, match="missing parameters.max_xtb_stages"):
        _required_param({"max_crest_candidates": 2}, "max_xtb_stages")
    with pytest.raises(ValueError, match="missing parameters.max_crest_candidates"):
        _required_param({}, "max_crest_candidates")
