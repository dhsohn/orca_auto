"""HTML presentation of vibrational summaries."""

from __future__ import annotations

import html

from ..frequencies import ModeSummary


def mode_section_html(
    summaries: tuple[ModeSummary, ...],
    alignment_label: str | None,
    *,
    frequency_calculation_found: bool = False,
) -> str:
    if not summaries:
        if frequency_calculation_found:
            return (
                '<p class="muted">Frequency values were parsed, but no usable normal-mode '
                "displacement vectors were available, so atom-level vibrational details cannot "
                "be shown.</p>"
            )
        return (
            '<p class="muted">No frequency calculation found at the final geometry in any '
            "attempt output, so no vibrational summary is available.</p>"
        )
    blocks = []
    for summary in summaries:
        kind = "imaginary mode" if summary.imaginary else "lowest real mode"
        freq_text = f"{summary.frequency_cm:.1f} cm&#8315;&#185;"
        atoms = ", ".join(
            f"{entry.element}{entry.atom_index} ({entry.displacement:.2f})"
            for entry in summary.top_atoms
        )
        alignment_html = ""
        if alignment_label is not None and summary.scan_alignment is not None:
            alignment_html = (
                f'<div class="sub">Alignment with {html.escape(alignment_label)}: '
                f"{summary.scan_alignment * 100:.0f}%</div>"
            )
        blocks.append(
            '<div class="mode">'
            f"<div><strong>Mode {summary.mode_index}</strong> &#183; {kind} &#183; "
            f"&#957; = {freq_text}</div>"
            f'<div class="sub">Top atom displacements (weighted norm): {html.escape(atoms)}'
            "</div>"
            f"{alignment_html}"
            "</div>"
        )
    return "".join(blocks)
