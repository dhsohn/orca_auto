from __future__ import annotations

import subprocess
from typing import Any

import pytest

from orca_auto import cli_systemd_evidence


@pytest.mark.parametrize(
    ("stamp", "expected_ns", "expected_epoch"),
    [
        ("Sat 2026-10-10 07:03:44.560773 UTC", 1_791_615_824_560_773_000, 1_791_615_824.560773),
        ("Sat 2026-10-10 07:03:44.000001 UTC", 1_791_615_824_000_001_000, 1_791_615_824.000001),
        ("Sat 2026-10-10 07:03:44.999999 UTC", 1_791_615_824_999_999_000, 1_791_615_824.999999),
        ("Sat 2026-10-10 07:03:45.000000 UTC", 1_791_615_825_000_000_000, 1_791_615_825.0),
        ("Sat 2026-10-10 07:03:44 UTC", 1_791_615_824_000_000_000, 1_791_615_824.0),
    ],
)
@pytest.mark.parametrize("line_ending", ["", "\n", "\r\n"])
def test_unit_start_preserves_systemd_precision(
    stamp: str, expected_ns: int, expected_epoch: float, line_ending: str
) -> None:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == [
            "systemctl",
            "show",
            "--property=ExecMainStartTimestamp",
            "--value",
            "--timestamp=us+utc",
            "worker.service",
        ]
        return subprocess.CompletedProcess(argv, 0, stdout=stamp + line_ending, stderr="")

    assert cli_systemd_evidence.unit_start_epoch_ns("worker.service", run=run) == expected_ns
    assert cli_systemd_evidence.unit_start_epoch("worker.service", run=run) == expected_epoch


@pytest.mark.parametrize(
    "stamp",
    [
        "",
        "n/a",
        "Sat 2026-10-10 07:03:44.560773 KST",
        "Sat 2026-10-10 07:03:44.5 UTC",
        "Sat 2026-10-10 07:03:44.5607731 UTC",
        "Sat 2026-10-10 07:03:44.560773 UTC extra",
        "Sat 2026-10-10 07:03:44.560773 UTC\nSat 2026-10-10 07:03:45 UTC",
        "Sat 2026-02-30 07:03:44.560773 UTC",
        "\nSat 2026-10-10 07:03:44.560773 UTC\n",
        "Sat 2026-10-10 07:03:44.560773 UTC\n\n",
        "Sat 2026-10-10 07:03:44.560773 UTC\r\n\r\n",
        "Sat 2026-10-10 07:03:44.560773",
        "Sat 2026-10 07:03:44.560773 UTC",
        "Sat 2026-10-10 07:03 UTC",
    ],
)
def test_unit_start_rejects_malformed_or_ambiguous_records(stamp: str) -> None:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stamp, stderr="")

    with pytest.raises(ValueError):
        cli_systemd_evidence.unit_start_epoch_ns("worker.service", run=run)


@pytest.mark.parametrize("failure", ["returncode", "stderr", "exception"])
def test_unit_start_rejects_failed_queries_despite_valid_timestamp(failure: str) -> None:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "exception":
            raise OSError("bus unavailable")
        return subprocess.CompletedProcess(
            argv,
            1 if failure == "returncode" else 0,
            stdout="Sat 2026-10-10 07:03:44.560773 UTC\n",
            stderr="bus warning" if failure == "stderr" else "",
        )

    with pytest.raises(ValueError):
        cli_systemd_evidence.unit_start_epoch_ns("worker.service", run=run)
