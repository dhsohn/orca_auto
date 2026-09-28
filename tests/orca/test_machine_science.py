from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import machine_observation as machine

FIXTURES = Path(__file__).parents[1] / "fixtures" / "orca_6_1_1"
SP_INPUT = "! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
SP_OUTPUT = (
    "SCF CONVERGED AFTER 2 CYCLES\nFINAL SINGLE POINT ENERGY -1.1\nORCA TERMINATED NORMALLY\n"
)


def _observation(
    tmp_path: Path, inp: bytes, out: bytes, status: str = "completed"
) -> dict[str, Any]:
    selected = tmp_path / "job.inp"
    output = tmp_path / "job.out"
    selected.write_bytes(inp)
    output.write_bytes(out)
    return machine.build_machine_observation(
        tmp_path,
        {
            "job": {"id": "test-science"},
            "input": {"primary_path": str(selected)},
            "status": {"state": status},
            "engine_payload": {
                "final_result": {"last_out_path": str(output), "reason": "normal_termination"},
            },
        },
    )


def test_machine_sp_fields_have_independent_literal_values(tmp_path: Path) -> None:
    observation = _observation(tmp_path, SP_INPUT.encode(), SP_OUTPUT.encode())
    assert observation["payload"]["data"]["results"]["science"] == {
        "status": "verified",
        "reason": "normal_termination",
        "energy_hartree": -1.1,
        "scf_converged": True,
        "optimization_converged": None,
        "frequencies_available": False,
        "imaginary_frequency_count": None,
        "geometry_scope": "single_point",
        "stationary_point": "unverified",
        "output_artifact": "orca-output",
        "evidence_lines": {"energy": 2, "scf": 1, "optimization": None},
    }
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["handoff"]["status"] == "ready"


@pytest.mark.parametrize(
    ("case", "state", "science_status", "energy", "scope", "stationary", "imaginary"),
    [
        ("h2_sp", "completed", "verified", -1.116759307204, "single_point", "unverified", None),
        ("water_opt_freq", "completed", "verified", -74.965901189921, "full", "minimum", 0),
        ("water_not_converged", "failed", "failed", -74.941321118008, "full", "unverified", None),
        (
            "nh3_ts_irc",
            "completed",
            "verified",
            -55.437665301787,
            "transition_state",
            "first_order_saddle",
            1,
        ),
    ],
)
def test_authentic_machine_science(
    tmp_path: Path,
    case: str,
    state: str,
    science_status: str,
    energy: float,
    scope: str,
    stationary: str,
    imaginary: int | None,
) -> None:
    observation = _observation(
        tmp_path,
        (FIXTURES / f"{case}.inp").read_bytes(),
        (FIXTURES / f"{case}.out").read_bytes(),
        state,
    )
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == science_status
    assert science["energy_hartree"] == pytest.approx(energy, abs=1e-10)
    assert science["geometry_scope"] == scope
    assert science["stationary_point"] == stationary
    assert science["imaginary_frequency_count"] == imaginary
    assert science["frequencies_available"] is (imaginary is not None)


def test_missing_evidence_never_becomes_consumable_success(tmp_path: Path) -> None:
    observation = _observation(tmp_path, SP_INPUT.encode(), b"ORCA TERMINATED NORMALLY\n")
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["energy_hartree"] is None
    assert science["imaginary_frequency_count"] is None
    assert science["stationary_point"] == "unverified"
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"


def test_constrained_ts_does_not_claim_full_saddle(tmp_path: Path) -> None:
    inp = "! HF STO-3G OptTS Freq\n%geom Constraints { B 0 1 C } end end\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n"
    out = "FINAL SINGLE POINT ENERGY -1.1\nTHE OPTIMIZATION HAS CONVERGED\nVIBRATIONAL FREQUENCIES\n 0: -400.0 cm**-1\nORCA TERMINATED NORMALLY\n"
    observation = _observation(tmp_path, inp.encode(), out.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["geometry_scope"] == "partial"
    assert science["stationary_point"] == "unverified"


@pytest.mark.parametrize("target", ["input", "orca-output"])
def test_receipt_then_changed_bytes_cannot_publish_science(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    original = machine._scientific_results

    def change_before_analysis(root: Path, artifacts: Any) -> dict[str, Any]:
        path = root / artifacts[target]["path"]
        path.write_text(
            SP_INPUT.replace("SP", "Opt")
            if target == "input"
            else SP_OUTPUT.replace("-1.1", "-2.2")
        )
        return original(root, artifacts)

    monkeypatch.setattr(machine, "_scientific_results", change_before_analysis)
    observation = _observation(tmp_path, SP_INPUT.encode(), SP_OUTPUT.encode())
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "unknown"
    assert science["energy_hartree"] is None
    assert observation["lifecycle"]["outcome"] == "uncertain"
    assert observation["handoff"]["status"] == "blocked"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-8-sig"])
def test_science_digest_binds_original_encoded_bytes(tmp_path: Path, encoding: str) -> None:
    observation = _observation(tmp_path, SP_INPUT.encode(), SP_OUTPUT.encode(encoding))
    science = observation["payload"]["data"]["results"]["science"]
    assert science["status"] == "verified"
    assert science["energy_hartree"] == -1.1
    assert science["evidence_lines"]["energy"] == 2
    assert observation["handoff"]["status"] == "ready"
