"""The completion verdict scans frequency sections without the report detail.

``out_analyzer`` calls :func:`scan_frequency_sections` with
``include_modes_and_geometry=False``. The section that counts, its
frequencies, its imaginary count and ``seen`` must be exactly what the
report-detail scan decides; only the normal-mode matrix and the geometry,
whose size grows with the square of the atom count, are left out.
"""

from __future__ import annotations

import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from orca_auto.orca.frequencies import FrequencySections, scan_frequency_sections
from orca_auto.orca.output_status import iter_output_lines

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "orca_6_1_1"

_FREQ = "VIBRATIONAL FREQUENCIES\n-----------------------\n\n"
_MODES = (
    "NORMAL MODES\n------------\n\n"
    "                  0          1\n"
    "      0       0.100000   0.200000\n"
    "      1       0.300000   0.400000\n\n"
)
_COORDS = (
    "CARTESIAN COORDINATES (ANGSTROEM)\n"
    "---------------------------------\n"
    "  H      0.000000    0.000000    0.000000\n"
    "  H      0.000000    0.000000    0.740000\n\n"
)
_ENERGY = "FINAL SINGLE POINT ENERGY      -1.117506\n"
_TS = "  0:      -420.00 cm**-1\n  1:       120.00 cm**-1\n\n"
_MIN = "  0:       300.00 cm**-1\n  1:       900.00 cm**-1\n\n"

_SYNTHETIC = {
    "ts-with-modes": _COORDS + _ENERGY + _FREQ + _TS + _MODES,
    "modes-before-frequencies": _COORDS + _ENERGY + _MODES + _FREQ + _TS,
    "superseded-by-later-energy": _ENERGY + _FREQ + _TS + _MODES + _COORDS + _ENERGY,
    "earlier-hessian-then-final": _ENERGY + _FREQ + _MIN + _MODES + _ENERGY + _FREQ + _TS,
    "last-header-empty": _ENERGY + _FREQ + _TS + _MODES + _FREQ + _MODES,
    "unsupported-value": _ENERGY + _FREQ + "  0:   NaN cm**-1\n" + _TS + _MODES,
    "unsupported-then-recovered": (
        _ENERGY + _FREQ + "  0:   NaN cm**-1\n" + _MODES + _FREQ + _MIN + _MODES
    ),
    "header-inside-modes": _ENERGY + _MODES.rstrip("\n") + "\n" + _FREQ + _TS,
    "echoed-and-commented": (
        _ENERGY + _FREQ + _TS + "| 12> VIBRATIONAL FREQUENCIES\n# NORMAL MODES\n" + _MODES
    ),
    "no-frequency-section": _COORDS + _ENERGY + _MODES,
    "empty": "",
}


def _outputs() -> Iterator[tuple[str, str]]:
    for path in sorted(_FIXTURES.glob("*.out")):
        yield path.name, path.read_text(encoding="utf-8")
    yield from sorted(_SYNTHETIC.items())


def _verdict(sections: FrequencySections) -> tuple[object, ...]:
    analysis = sections.analysis
    if analysis is None:
        return (sections.seen, None, None)
    return (sections.seen, analysis.frequencies, analysis.imaginary_count())


@pytest.mark.parametrize(("name", "text"), list(_outputs()), ids=lambda value: str(value)[:40])
def test_verdict_scan_decides_exactly_like_the_detail_scan(name: str, text: str) -> None:
    detail = scan_frequency_sections(iter_output_lines(text))
    verdict = scan_frequency_sections(iter_output_lines(text), include_modes_and_geometry=False)

    assert _verdict(verdict) == _verdict(detail), name
    if verdict.analysis is not None:
        assert verdict.analysis.mode_matrix == {}
        assert verdict.analysis.atoms == ()


def test_detail_scan_still_collects_modes_and_geometry() -> None:
    text = (_FIXTURES / "water_opt_freq.out").read_text(encoding="utf-8")

    analysis = scan_frequency_sections(iter_output_lines(text)).analysis

    assert analysis is not None
    assert len(analysis.atoms) == 3
    assert len(analysis.mode_matrix) == 3 * len(analysis.atoms)


def _square_modes_output(size: int) -> Iterator[str]:
    """One line per item, as a file handle yields them, never the whole text."""
    yield from iter_output_lines(_ENERGY + _FREQ)
    for mode in range(size):
        yield f"  {mode}:   {100.0 + mode:.2f} cm**-1\n"
    yield "\n"
    yield from iter_output_lines("NORMAL MODES\n------------\n\n")
    for start in range(0, size, 6):
        columns = range(start, min(start + 6, size))
        yield "      " + "  ".join(str(col) for col in columns) + "\n"
        for row in range(size):
            yield f"  {row}  " + "  ".join("0.123456" for _ in columns) + "\n"
    yield "\n"


def _peak_bytes(size: int, *, include_modes_and_geometry: bool) -> int:
    tracemalloc.start()
    try:
        scan_frequency_sections(
            _square_modes_output(size), include_modes_and_geometry=include_modes_and_geometry
        )
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_verdict_scan_does_not_hold_the_mode_matrix() -> None:
    size = 180
    detail = _peak_bytes(size, include_modes_and_geometry=True)
    verdict = _peak_bytes(size, include_modes_and_geometry=False)

    # The detail scan keeps size**2 coefficients (two copies at section close);
    # the verdict scan keeps only the size frequencies.
    assert verdict * 10 < detail
