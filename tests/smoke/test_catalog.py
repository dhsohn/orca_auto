from __future__ import annotations

import pytest

from orca_auto.smoke.catalog import scenarios_for_profile


def test_real_orca_profile_selects_retained_h2_acceptance() -> None:
    scenarios = scenarios_for_profile("real-orca")

    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert scenario.scenario_id == "orca_real_h2_single_point"
    assert scenario.profile == "real-orca"
    assert scenario.expected_status == "completed"
    assert scenario.pytest_selector.endswith(
        "test_real_orca_h2_single_point_acceptance_when_configured"
    )
    assert {"job_report.html", "si_block.md", "h2.out"}.issubset(scenario.required_artifacts)


def test_all_profile_includes_fake_and_both_real_engine_lanes() -> None:
    profiles = {scenario.profile for scenario in scenarios_for_profile("all")}

    assert profiles == {"fake", "real-orca", "real-xtb"}


def test_unknown_profile_lists_real_orca_choice() -> None:
    with pytest.raises(ValueError, match="real-orca"):
        scenarios_for_profile("unknown")
