from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from orca_auto.orca import summary
from orca_auto.orca.run_snapshot import RunSnapshot


class _FrozenDateTime(datetime):
    @classmethod
    def frozen_now(cls) -> datetime:
        return cls(2026, 1, 10, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        current = cls.frozen_now()
        if tz is None:
            return current
        return current.astimezone(tz)

    @classmethod
    def fromtimestamp(cls, timestamp, tz=None):  # type: ignore[override]
        current = datetime.fromtimestamp(timestamp, tz=tz)
        return cls(
            current.year,
            current.month,
            current.day,
            current.hour,
            current.minute,
            current.second,
            current.microsecond,
            tzinfo=current.tzinfo,
        )


class _FakeRaw:
    def __init__(self) -> None:
        self.calls = 0

    def decode(self, encoding: str, errors: str = "replace") -> str:
        self.calls += 1
        if self.calls <= 4:
            raise LookupError(f"codec {encoding} unavailable")
        return "decoded via fallback"


class _FakeBinaryHandle:
    def __init__(self, raw: _FakeRaw) -> None:
        self.raw = raw

    def __enter__(self) -> _FakeBinaryHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        return False

    def seek(self, *_args) -> None:
        return None

    def tell(self) -> int:
        return 64

    def read(self):
        return self.raw


def _snapshot(
    reaction_dir: Path,
    *,
    name: str = "rxn",
    status: str = "running",
    latest_out_path: Path | None = None,
    selected_inp_name: str = "calc.inp",
    final_reason: str = "",
    started_at: str = "2026-01-10T10:00:00+00:00",
    completed_at: str = "2026-01-10T11:00:00+00:00",
) -> RunSnapshot:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    return RunSnapshot(
        key=f"key-{name}",
        name=name,
        reaction_dir=reaction_dir,
        run_id=f"run-{name}",
        status=status,
        started_at=started_at,
        updated_at=completed_at,
        completed_at=completed_at,
        selected_inp_name=selected_inp_name,
        attempts=1,
        latest_out_path=latest_out_path,
        final_reason=final_reason,
        elapsed=0.0,
        elapsed_text="0m",
    )


def test_human_duration_elapsed_and_updated_text_cover_formatting_branches(tmp_path: Path) -> None:
    recent = tmp_path / "recent.out"
    hourly = tmp_path / "hourly.out"
    daily = tmp_path / "daily.out"
    for path in (recent, hourly, daily):
        path.write_text("x", encoding="utf-8")

    with patch("orca_auto.orca.summary.datetime", _FrozenDateTime):
        os.utime(recent, (0, _FrozenDateTime.frozen_now().timestamp() - 30 * 60))
        os.utime(hourly, (0, _FrozenDateTime.frozen_now().timestamp() - (2 * 3600 + 5 * 60)))
        os.utime(daily, (0, _FrozenDateTime.frozen_now().timestamp() - (2 * 86400 + 3 * 3600)))

        assert summary._human_duration(90) == "1m"
        assert summary._human_duration(3660) == "1h 01m"
        assert summary._human_duration(90000) == "1d 1h"
        assert summary._elapsed_from_started("bad timestamp") == "n/a"
        assert summary._updated_ago_text(recent) == "30m"
        assert summary._updated_ago_text(hourly) == "2h 05m"
        assert summary._updated_ago_text(daily) == "2d 3h"

    assert summary._updated_ago_text(tmp_path / "missing.out") == "n/a"
    assert summary._human_bytes(1536) == "1.5 KB"


def test_scan_cwd_process_counts_handles_proc_absence_and_filters(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inside = allowed_root / "job"
    outside = tmp_path / "other"
    inside.mkdir()
    outside.mkdir()

    with patch.object(Path, "is_dir", return_value=False):
        assert summary._scan_cwd_process_counts(allowed_root) == {}

    entries = [Path("/proc/abc"), Path("/proc/111"), Path("/proc/222"), Path("/proc/333")]

    def _readlink(path: Path) -> str:
        path_text = str(path)
        if "111" in path_text:
            return str(inside)
        if "222" in path_text:
            return str(outside)
        raise OSError("missing cwd")

    with (
        patch.object(Path, "is_dir", return_value=True),
        patch.object(
            Path,
            "iterdir",
            return_value=entries,
        ),
        patch("orca_auto.orca.summary.os.readlink", side_effect=_readlink),
    ):
        counts = summary._scan_cwd_process_counts(allowed_root)

    assert counts == {inside.resolve(): 1}


def test_read_tail_and_last_non_empty_line_cover_error_empty_and_decode_fallback(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.out"
    empty.write_bytes(b"")
    text = tmp_path / "text.out"
    text.write_text("\n\n  first line  \n\n  second   line   \n", encoding="utf-8")

    assert summary._read_tail_text(tmp_path / "missing.out") == ""
    assert summary._read_tail_text(empty) == ""
    assert summary._last_non_empty_line(tmp_path / "missing.out") == "(tail line not found)"
    assert summary._last_non_empty_line(text) == "second line"

    raw = _FakeRaw()
    with patch.object(Path, "open", return_value=_FakeBinaryHandle(raw)):
        assert summary._read_tail_text(Path("virtual.out")) == "decoded via fallback"


def test_extract_geometry_maxiter_and_eta_summary_cover_remaining_time_branches(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "calc.out"
    out_path.write_text(
        "\n".join(
            [
                "Max. no of cycles        MaxIter  .... 40",
                "Max. no of cycles        MaxIter  .... 174",
            ]
        ),
        encoding="utf-8",
    )

    with patch("orca_auto.orca.summary.datetime", _FrozenDateTime):
        assert summary._extract_geometry_maxiter(out_path) == 174
        assert summary._extract_geometry_maxiter(tmp_path / "missing.out") is None
        assert (
            summary._eta_summary(cycle=None, maxiter=10, started_at="2026-01-10T10:00:00+00:00")
            == "n/a"
        )
        assert summary._eta_summary(
            cycle=2, maxiter=8, started_at="2026-01-08T12:00:00+00:00"
        ).startswith("6d 0h")
        assert summary._eta_summary(
            cycle=4, maxiter=10, started_at="2026-01-10T10:00:00+00:00"
        ).startswith("3h 0m")
        assert summary._eta_summary(
            cycle=1, maxiter=2, started_at="2026-01-10T11:00:00+00:00"
        ).startswith("1h 0m")
        assert summary._eta_summary(cycle=1, maxiter=2, started_at="bad") == "n/a"


def test_build_progress_snapshot_handles_missing_outputs_and_parser_failure(tmp_path: Path) -> None:
    missing_run = _snapshot(tmp_path / "missing_run", latest_out_path=None)
    missing_snapshot = summary._build_progress_snapshot(
        missing_run, {missing_run.reaction_dir.resolve(): 3}
    )

    assert missing_snapshot.out_name == "n/a"
    assert missing_snapshot.proc_count == 3
    assert missing_snapshot.tail_text == "(tail line not found)"

    out_path = tmp_path / "running_run" / "calc.out"
    out_path.parent.mkdir(parents=True)
    out_path.write_text(
        "FINAL SINGLE POINT ENERGY      -99.123456\nSCF still running\n",
        encoding="utf-8",
    )
    running_run = _snapshot(
        out_path.parent, latest_out_path=out_path, started_at="2026-01-10T10:00:00+00:00"
    )

    with (
        patch("orca_auto.orca.summary.parse_opt_progress", side_effect=RuntimeError("bad parse")),
        patch(
            "orca_auto.orca.summary._updated_ago_text",
            return_value="5m",
        ),
    ):
        progress_snapshot = summary._build_progress_snapshot(running_run, {})

    assert progress_snapshot.energy_hartree == -99.123456
    assert progress_snapshot.cycle is None
    assert progress_snapshot.updated_text == "5m ago"
    assert progress_snapshot.tail_text == "SCF still running"


def test_section_formatters_cover_empty_and_truncated_sections(tmp_path: Path) -> None:
    assert summary._format_running_section([], {}) is None
    assert summary._format_attention_section([], []) is None

    active: list[RunSnapshot] = []
    attention: list[RunSnapshot] = []
    for index in range(9):
        run_dir = tmp_path / f"rxn_{index}"
        active.append(
            _snapshot(run_dir, name=f"rxn_{index}", selected_inp_name=f"calc_{index}.inp")
        )
        attention.append(
            _snapshot(run_dir, name=f"failed_{index}", status="failed", final_reason="boom")
        )

    (active[0].reaction_dir / summary.LOCK_FILE_NAME).write_text("{}", encoding="utf-8")
    fake_progress = summary.ProgressSnapshot(
        cycle=7,
        energy_hartree=-10.5,
        out_name="calc.out",
        out_size_text="1.0 KB",
        updated_text="5m ago",
        proc_count=None,
        eta_text="45m (maxiter=10, rate=6.00 cyc/h)",
        tail_text="still running",
    )
    with patch("orca_auto.orca.summary._build_progress_snapshot", return_value=fake_progress):
        running_text = summary._format_running_section(active, {})

    attention_text = summary._format_attention_section(attention, [])
    assert running_text is not None
    assert "showing 8/9" in running_text
    assert "run.lock present" in running_text
    assert attention_text is not None
    assert "showing 8/9" in attention_text
