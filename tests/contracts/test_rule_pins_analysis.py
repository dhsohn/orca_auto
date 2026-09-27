"""Rule pin: the output analyzer verdict for every corpus ``.out`` file.

Each file of ``pins/out_corpus`` is analyzed as-is (the buffered read) and
padded past 256 KiB with leading space-only lines (the streaming read), in each
completion mode. The table is ``pins/analysis_out_analyzer.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.orca.completion_rules import CompletionMode
from orca_auto.orca.out_analyzer import analyze_output
from tests.contracts.normalize import PINS_DIR, assert_pin

CORPUS_DIR = PINS_DIR / "out_corpus"
# 257 KiB of space-only lines: past both buffered-read limits (64 KiB, 256 KiB TS).
_PADDING = (b" " * 1023 + b"\n") * 257
_MODES = {
    "opt": CompletionMode(kind="opt", require_irc=False, route_line="! Opt"),
    "ts": CompletionMode(kind="ts", require_irc=False, route_line="! OptTS"),
    "ts_irc": CompletionMode(kind="ts", require_irc=True, route_line="! OptTS IRC"),
}


def _verdict(out_path: Path, mode: CompletionMode, label: str) -> dict[str, Any]:
    analysis = analyze_output(out_path, mode)
    markers = dict(analysis.markers)
    markers["out_path"] = label
    return {"status": analysis.status.value, "reason": analysis.reason, "markers": markers}


def test_out_analyzer_verdicts(tmp_path: Path) -> None:
    table: dict[str, Any] = {}
    for source in sorted(CORPUS_DIR.glob("*.out")):
        padded = tmp_path / source.name
        padded.write_bytes(_PADDING + source.read_bytes())
        for mode_name, mode in _MODES.items():
            table[f"{source.name}|{mode_name}|as_is"] = _verdict(source, mode, source.name)
            table[f"{source.name}|{mode_name}|padded"] = _verdict(padded, mode, source.name)
    assert len(table) == 2 * len(_MODES) * len(list(CORPUS_DIR.glob("*.out")))
    assert_pin("analysis_out_analyzer.json", table)
