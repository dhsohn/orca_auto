from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SmokeScenario:
    scenario_id: str
    surface: str
    description: str
    pytest_selector: str
    expected_status: str
    required_artifacts: tuple[str, ...]
    profile: str = "fake"
    timeout_seconds: float = 180.0


_FAKE_SCENARIOS: tuple[SmokeScenario, ...] = (
    SmokeScenario(
        scenario_id="orca_opt_success",
        surface="orca-standalone",
        description="Public run-dir and foreground ORCA worker complete one fake Opt job.",
        pytest_selector=(
            "tests/integration/test_orca_worker_smoke.py::"
            "test_orca_queue_worker_run_once_executes_fake_orca_child_lifecycle"
        ),
        expected_status="completed",
        required_artifacts=(
            "job_state.json",
            "job_report.json",
            "job_report.md",
            "job_report.html",
            "si_block.md",
        ),
    ),
    SmokeScenario(
        scenario_id="orca_false_success_rejected",
        surface="orca-standalone",
        description="Return code zero without ORCA normal termination must fail closed.",
        pytest_selector=(
            "tests/integration/test_orca_worker_smoke.py::"
            "test_orca_queue_worker_rejects_return_code_zero_without_normal_marker"
        ),
        expected_status="failed",
        required_artifacts=(
            "job_state.json",
            "job_report.json",
            "job_report.md",
            "job_report.html",
        ),
    ),
    SmokeScenario(
        scenario_id="xtb_md_nvt_success",
        surface="xtb-md-standalone",
        description="Fake xTB-MD NVT generation completes with validated retained artifacts.",
        pytest_selector=(
            "tests/xtb_md/test_submission_runtime.py::test_fake_xtb_md_nvt_nve_smoke[nvt]"
        ),
        expected_status="completed",
        required_artifacts=("job_state.json", "job_report.json", "job_report.md", "xtb.trj"),
    ),
    SmokeScenario(
        scenario_id="xtb_md_nve_success",
        surface="xtb-md-standalone",
        description="Fake xTB-MD NVE generation completes with validated retained artifacts.",
        pytest_selector=(
            "tests/xtb_md/test_submission_runtime.py::test_fake_xtb_md_nvt_nve_smoke[nve]"
        ),
        expected_status="completed",
        required_artifacts=("job_state.json", "job_report.json", "job_report.md", "xtb.trj"),
    ),
    SmokeScenario(
        scenario_id="xtb_md_false_success_rejected",
        surface="xtb-md-standalone",
        description="xTB return code zero and xtbmdok cannot override a fatal MD marker.",
        pytest_selector=(
            "tests/xtb_md/test_submission_runtime.py::"
            "test_false_success_is_terminal_failure_not_completion"
        ),
        expected_status="failed",
        required_artifacts=("job_state.json", "job_report.json", "job_report.md", "xtb.trj"),
    ),
    SmokeScenario(
        scenario_id="reaction_ts_search_success",
        surface="workflow",
        description="Reaction workflow completes fake CREST, xTB path, and ORCA TS stages.",
        pytest_selector=(
            "tests/integration/test_reaction_workflow_smoke.py::"
            "test_reaction_ts_workflow_executes_fake_crest_xtb_and_orca_full_lifecycle"
        ),
        expected_status="completed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "workflow_si.md",
            "si_data.csv",
            "job_report.json",
            "job_report.html",
            "si_block.md",
        ),
    ),
    SmokeScenario(
        scenario_id="reaction_ts_search_failure",
        surface="workflow",
        description="Invalid xTB TS handoff fails the reaction workflow before ORCA execution.",
        pytest_selector=(
            "tests/integration/test_reaction_workflow_smoke.py::"
            "test_reaction_ts_workflow_terminalizes_failed_xtb_handoff"
        ),
        expected_status="failed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "job_report.json",
            "job_report.md",
        ),
    ),
    SmokeScenario(
        scenario_id="conformer_screening_success",
        surface="workflow",
        description="Conformer workflow completes fake CREST and every selected ORCA child.",
        pytest_selector=(
            "tests/integration/test_workflow_handoff_smoke.py::"
            "test_conformer_screening_workflow_completes_fake_engine_lifecycle"
        ),
        expected_status="completed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "workflow_si.md",
            "si_data.csv",
            "job_report.json",
            "job_report.html",
            "si_block.md",
        ),
    ),
    SmokeScenario(
        scenario_id="conformer_screening_failure",
        surface="workflow",
        description="All selected ORCA conformers failing terminalizes the workflow.",
        pytest_selector=(
            "tests/integration/test_workflow_handoff_smoke.py::"
            "test_conformer_screening_workflow_terminalizes_all_orca_failures"
        ),
        expected_status="failed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "workflow_si.md",
            "si_data.csv",
            "job_report.json",
            "job_report.html",
        ),
    ),
    SmokeScenario(
        scenario_id="scan_ts_search_success",
        surface="workflow",
        description="Relaxed scan fan-out and a fake OptTS child complete the scan workflow.",
        pytest_selector=(
            "tests/integration/test_scan_workflow_smoke.py::"
            "test_scan_ts_workflow_completes_fake_orca_lifecycle"
        ),
        expected_status="completed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "workflow_si.md",
            "si_data.csv",
            "job_report.json",
            "job_report.html",
            "si_block.md",
        ),
    ),
    SmokeScenario(
        scenario_id="scan_ts_search_failure",
        surface="workflow",
        description="A barrierless terminal scan fails with scan_profile_no_barrier.",
        pytest_selector=(
            "tests/integration/test_scan_workflow_smoke.py::"
            "test_scan_ts_workflow_terminalizes_barrierless_profile"
        ),
        expected_status="failed",
        required_artifacts=(
            "workflow.json",
            "workflow_report.html",
            "workflow_si.md",
            "si_data.csv",
            "job_report.json",
            "job_report.html",
        ),
    ),
)

_REAL_XTB_SCENARIOS: tuple[SmokeScenario, ...] = (
    SmokeScenario(
        scenario_id="xtb_md_real_nvt_nve",
        surface="xtb-md-standalone",
        description="Opt-in real xTB 6.7.1 two-step NVT and NVE acceptance.",
        pytest_selector=(
            "tests/xtb_md/test_submission_runtime.py::"
            "test_real_xtb_671_two_step_acceptance_when_configured"
        ),
        expected_status="completed",
        required_artifacts=("job_state.json", "job_report.json", "job_report.md", "xtb.trj"),
        profile="real-xtb",
        timeout_seconds=300.0,
    ),
)

_REAL_ORCA_SCENARIOS: tuple[SmokeScenario, ...] = (
    SmokeScenario(
        scenario_id="orca_real_h2_single_point",
        surface="orca-standalone",
        description="Opt-in real ORCA H2 HF/STO-3G single-point acceptance.",
        pytest_selector=(
            "tests/integration/test_orca_worker_smoke.py::"
            "test_real_orca_h2_single_point_acceptance_when_configured"
        ),
        expected_status="completed",
        required_artifacts=(
            "job_state.json",
            "job_report.json",
            "job_report.md",
            "job_report.html",
            "si_block.md",
            "h2.out",
        ),
        profile="real-orca",
        timeout_seconds=300.0,
    ),
)


def scenarios_for_profile(profile: str) -> tuple[SmokeScenario, ...]:
    normalized = profile.strip().lower()
    if normalized == "fake":
        return _FAKE_SCENARIOS
    if normalized == "real-xtb":
        return _REAL_XTB_SCENARIOS
    if normalized == "real-orca":
        return _REAL_ORCA_SCENARIOS
    if normalized == "all":
        return (*_FAKE_SCENARIOS, *_REAL_ORCA_SCENARIOS, *_REAL_XTB_SCENARIOS)
    raise ValueError("profile must be one of: fake, real-orca, real-xtb, all")


__all__ = ["SmokeScenario", "scenarios_for_profile"]
