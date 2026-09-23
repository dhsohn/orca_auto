from __future__ import annotations

import io
import tracemalloc

import pytest

from orca_auto.orca import output_status


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
@pytest.mark.parametrize("trailing", [False, True])
def test_output_line_iterator_preserves_universal_newlines(separator: str, trailing: bool) -> None:
    text = separator.join(["", "# input echo", "ORCA TERMINATED NORMALLY", "αβ"]) + (
        separator if trailing else ""
    )
    assert list(output_status.iter_output_lines(text)) == list(io.StringIO(text, newline=None))
    assert list(output_status.iter_output_lines("")) == []


def test_termination_scan_does_not_clone_the_full_output() -> None:
    text = " bounded irrelevant output\n" * 100_000 + "ORCA TERMINATED NORMALLY\n"
    tracemalloc.start()
    try:
        assert output_status.has_normal_termination(text)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < len(text) // 2
