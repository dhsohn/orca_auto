"""Self-contained HTML job reports written next to ``machine.json``.

``write_job_html_report`` builds a shared report context from the selected
input and then composes the applicable sections: optimization convergence,
frequency validation, scan profiles, NEB paths, IRC paths, SP energy/SI blocks,
and a single attempt chain. Other non-stationary path/dynamics jobs (plain NEB,
MD) get no HTML report. Report generation must never break run finalization, so
every error is logged and swallowed.
"""

from __future__ import annotations

from .publication import write_job_html_report

__all__ = ["write_job_html_report"]
