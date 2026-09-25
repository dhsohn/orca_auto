from __future__ import annotations

RUN_STATE_FILE = "job_state.json"
RUN_REPORT_JSON_FILE = "machine.json"
RUN_REPORT_HTML_FILE = "job_report.html"
EXECUTION_PROVENANCE_FILE = "execution_provenance.json"
MAX_RUN_ARTIFACT_JSON_BYTES = 64 * 1024 * 1024
SI_BLOCK_MD_FILE = "si_block.md"
QUEUE_FILE = "queue.json"

__all__ = [
    "EXECUTION_PROVENANCE_FILE",
    "MAX_RUN_ARTIFACT_JSON_BYTES",
    "QUEUE_FILE",
    "RUN_REPORT_HTML_FILE",
    "RUN_REPORT_JSON_FILE",
    "RUN_STATE_FILE",
    "SI_BLOCK_MD_FILE",
]
