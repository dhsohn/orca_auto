from __future__ import annotations

from orca_auto.orca.machine_observation import machine_code


def test_machine_code_caps_long_dynamic_reasons_without_losing_identity() -> None:
    first = machine_code("orca_auto", "A" * 500, fallback="operation_failed")
    second = machine_code("orca_auto", "B" * 500, fallback="operation_failed")

    assert len(first) == 200
    assert first.startswith("orca_auto/aaaa")
    assert first == machine_code("orca_auto", "A" * 500, fallback="operation_failed")
    assert first != second
