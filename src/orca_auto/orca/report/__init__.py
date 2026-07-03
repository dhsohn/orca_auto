"""Self-contained HTML job reports written next to ``job_report.md``.

``write_job_html_report`` picks the report flavor from the selected input's
route lines: ScanTS jobs get the scan-profile report, other TS routes
(OptTS/NEB-TS) and plain Opt jobs get the optimization report. Jobs without an
optimization (single points, bare Freq) get no HTML report. Report generation
must never break run finalization, so every error is logged and swallowed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE
from orca_auto.core.utils.persistence import atomic_write_text

from ..completion_rules import TS_ROUTE_RE
from ..input_blocks import file_route_lines
from ..scants import input_uses_scants
from .frequencies import FrequencyAnalysis, parse_frequency_analysis
from .opt import OptReportData, collect_opt_report_data, render_opt_report_html
from .scants import ScantsReportData, collect_scants_report_data, render_scants_report_html

logger = logging.getLogger(__name__)

_OPT_ROUTE_RE = re.compile(r"\bOPT\b", re.IGNORECASE)


def _render_job_report(reaction_dir: Path, state: Mapping[str, Any]) -> str | None:
    selected_raw = str(state.get("selected_inp") or "").strip()
    if not selected_raw:
        return None
    selected_inp = Path(selected_raw)

    if input_uses_scants(selected_inp):
        scants_data = collect_scants_report_data(reaction_dir, state)
        return None if scants_data is None else render_scants_report_html(scants_data)

    routes = " ".join(file_route_lines(selected_inp))
    if TS_ROUTE_RE.search(routes):
        kind = "ts"
    elif _OPT_ROUTE_RE.search(routes):
        kind = "opt"
    else:
        return None
    opt_data = collect_opt_report_data(reaction_dir, state, kind=kind)
    return None if opt_data is None else render_opt_report_html(opt_data)


def write_job_html_report(reaction_dir: Path, state: Mapping[str, Any]) -> Path | None:
    """Write ``job_report.html``; ``None`` when the job type has no HTML report."""
    try:
        rendered = _render_job_report(reaction_dir, state)
        if rendered is None:
            return None
        path = reaction_dir / RUN_REPORT_HTML_FILE
        atomic_write_text(path, rendered)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("Job HTML report generation failed for %s", reaction_dir, exc_info=True)
        return None


__all__ = [
    "FrequencyAnalysis",
    "OptReportData",
    "ScantsReportData",
    "collect_opt_report_data",
    "collect_scants_report_data",
    "parse_frequency_analysis",
    "render_opt_report_html",
    "render_scants_report_html",
    "write_job_html_report",
]
