"""Self-contained HTML job reports written next to ``job_report.md``.

``write_job_html_report`` picks the report flavor from the selected input:
ScanTS jobs and plain relaxed scans (Opt route + ``%geom Scan`` block) get the
scan-profile report, other TS routes (OptTS/NEB-TS) and plain Opt jobs get the
optimization report, and single points / bare Freq jobs — the same set that
gets an ``"sp"`` SI block — get the single-point report. Non-stationary
path/dynamics jobs (IRC, plain NEB, MD) get no HTML report. Report generation
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
from ..scants import first_scan_coordinate_spec, input_uses_scants
from .frequencies import FrequencyAnalysis, parse_frequency_analysis
from .opt import OptReportData, collect_opt_report_data, render_opt_report_html
from .scants import ScantsReportData, collect_scants_report_data, render_scants_report_html
from .si import structure_kind
from .sp import SpReportData, collect_sp_report_data, render_sp_report_html

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
        # A plain relaxed scan (Opt route + %geom Scan block) is about the
        # energy profile, not the convergence trace: use the scan report.
        if first_scan_coordinate_spec(selected_inp) is not None:
            scan_data = collect_scants_report_data(reaction_dir, state, kind="scan")
            return None if scan_data is None else render_scants_report_html(scan_data)
        kind = "opt"
    else:
        # No optimization in the route: single points and bare Freq jobs get
        # the SP report; non-stationary jobs (IRC, plain NEB, MD) get none.
        if structure_kind(selected_inp) != "sp":
            return None
        sp_data = collect_sp_report_data(reaction_dir, state)
        return None if sp_data is None else render_sp_report_html(sp_data)
    opt_data = collect_opt_report_data(reaction_dir, state, kind=kind)
    return None if opt_data is None else render_opt_report_html(opt_data)


def write_job_html_report(reaction_dir: Path, state: Mapping[str, Any]) -> Path | None:
    """Write ``job_report.html``; ``None`` when the job type has no HTML report.

    When the current job type has no HTML report, any ``job_report.html`` left
    over from a previous job in a reused reaction dir is removed so downstream
    links (e.g. the workflow report) cannot surface an obsolete report. The
    exception path deliberately does NOT remove it: a transient parse error
    must not destroy the last valid report.
    """
    path = reaction_dir / RUN_REPORT_HTML_FILE
    try:
        rendered = _render_job_report(reaction_dir, state)
        if rendered is None:
            path.unlink(missing_ok=True)
            return None
        atomic_write_text(path, rendered)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("Job HTML report generation failed for %s", reaction_dir, exc_info=True)
        return None


__all__ = [
    "FrequencyAnalysis",
    "OptReportData",
    "ScantsReportData",
    "SpReportData",
    "collect_opt_report_data",
    "collect_scants_report_data",
    "collect_sp_report_data",
    "parse_frequency_analysis",
    "render_opt_report_html",
    "render_scants_report_html",
    "render_sp_report_html",
    "write_job_html_report",
]
