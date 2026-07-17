"""Self-contained HTML job reports written next to ``job_report.md``.

``write_job_html_report`` builds a shared report context from the selected
input and then composes the applicable sections: optimization convergence,
frequency validation, scan profiles, NEB paths, IRC paths, SP energy/SI blocks,
and a single attempt chain. Other non-stationary path/dynamics jobs (plain NEB,
MD) get no HTML report. Report generation must never break run finalization, so
every error is logged and swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE
from orca_auto.core.engine_process import atomic_write_confined_bytes
from orca_auto.core.utils.persistence import atomic_write_text

from .composer import collect_html_report_parts, compose_job_report_html
from .frequencies import FrequencyAnalysis, parse_frequency_analysis
from .irc import (
    IrcReportData,
    collect_irc_report_data,
    input_uses_irc,
    render_irc_report_html,
)
from .neb import NebReportData, collect_neb_report_data, render_neb_report_html
from .opt import OptReportData, collect_opt_report_data, render_opt_report_html
from .scants import ScantsReportData, collect_scants_report_data, render_scants_report_html
from .sp import SpReportData, collect_sp_report_data, render_sp_report_html

logger = logging.getLogger(__name__)


def _render_job_report(reaction_dir: Path, state: Mapping[str, Any]) -> str | None:
    return compose_job_report_html(reaction_dir, state)


def write_job_html_report(
    reaction_dir: Path,
    state: Mapping[str, Any],
    *,
    generation_target: tuple[Path, tuple[int, int]] | None = None,
) -> Path | None:
    """Write ``job_report.html``; ``None`` when the job type has no HTML report.

    With a verified ``generation_target`` the report lands inside the execution
    generation and any pre-relocation root copy is removed; without one (legacy
    states, runs that never bound a generation) the job root keeps the report.
    When the current job type has no HTML report, any ``job_report.html`` left
    over from a previous job in a reused reaction dir is removed so downstream
    links (e.g. the workflow report) cannot surface an obsolete report. The
    exception path deliberately does NOT remove it: a transient parse error
    must not destroy the last valid report.
    """
    target_dir = generation_target[0] if generation_target is not None else reaction_dir
    path = target_dir / RUN_REPORT_HTML_FILE
    root_path = reaction_dir / RUN_REPORT_HTML_FILE
    try:
        rendered = _render_job_report(reaction_dir, state)
        if rendered is None:
            path.unlink(missing_ok=True)
            if generation_target is not None:
                root_path.unlink(missing_ok=True)
            return None
        if generation_target is not None:
            atomic_write_confined_bytes(
                generation_target[0],
                path,
                rendered.encode("utf-8"),
                label="ORCA generation artifact",
                mode=0o600,
                expected_parent_identity=generation_target[1],
            )
            root_path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, rendered)
        return path
    except Exception:  # noqa: BLE001
        logger.warning("Job HTML report generation failed for %s", reaction_dir, exc_info=True)
        return None


__all__ = [
    "FrequencyAnalysis",
    "IrcReportData",
    "NebReportData",
    "OptReportData",
    "ScantsReportData",
    "SpReportData",
    "collect_html_report_parts",
    "collect_irc_report_data",
    "collect_neb_report_data",
    "collect_opt_report_data",
    "collect_scants_report_data",
    "collect_sp_report_data",
    "compose_job_report_html",
    "input_uses_irc",
    "parse_frequency_analysis",
    "render_irc_report_html",
    "render_neb_report_html",
    "render_opt_report_html",
    "render_scants_report_html",
    "render_sp_report_html",
    "write_job_html_report",
]
